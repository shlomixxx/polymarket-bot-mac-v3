"""Unit tests for engine/binance_exchange.py — the Binance USDⓈ-M Futures wrapper.

Every test uses a MockFuturesClient (records calls, returns canned exchangeInfo /
positions / fills). NO real network, NO real keys. Proves the SAFETY-CRITICAL
behaviours the cockpit depends on:

  * qty rounds DOWN to the lot step (never over-sizes past what risk_engine approved);
  * min-notional is respected;
  * set_leverage_isolated sets ISOLATED + leverage and SWALLOWS -4046;
  * place_market returns a fill (avg price + real fee from fills[]);
  * place_algo_stop uses the ALGO path with closePosition + MARK_PRICE + priceProtect;
  * market_close is reduceOnly and closes in the opposite direction;
  * NO method exists that could withdraw / transfer money off the account.
"""
from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

import binance_exchange
from binance_exchange import BinanceFuturesClient


# ---------------------------------------------------------------------------
# Mock connector — duck-types the binance-futures-connector UMFutures surface.
# ---------------------------------------------------------------------------

class MockClientError(Exception):
    """Mimics the connector's ClientError (carries an .error_code)."""

    def __init__(self, error_code: int, msg: str = ""):
        super().__init__(msg or f"binance error {error_code}")
        self.error_code = error_code


# canned exchangeInfo: BTCUSDT with a 0.001 lot step, 0.10 tick, 100 min-notional
_CANNED_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
            ],
        },
        {  # a second symbol to prove we pick the right one
            "symbol": "ETHUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "20"},
            ],
        },
    ]
}


class MockFuturesClient:
    """Records every call; returns canned data. Lets each method be told to raise."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        # position state the read methods will report
        self.position_rows = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.0",
                "entryPrice": "0.0",
                "leverage": "3",
                "unRealizedProfit": "0.0",
                "liquidationPrice": "0",
            }
        ]
        # error injection: method name -> exception to raise
        self.raise_on: dict[str, Exception] = {}
        # canned new_order fill
        self.fill_response = {
            "orderId": 555,
            "status": "FILLED",
            "executedQty": "0.005",
            "avgPrice": "0",
            "fills": [
                {"price": "60000.0", "qty": "0.003", "commission": "0.108"},
                {"price": "60010.0", "qty": "0.002", "commission": "0.072"},
            ],
        }
        self.algo_orders: list[dict] = []

    def _rec(self, name, **kw):
        self.calls.append((name, kw))
        if name in self.raise_on:
            raise self.raise_on[name]

    # --- read surface ---
    def account(self):
        self._rec("account")
        return {"totalWalletBalance": "1000.0", "availableBalance": "950.0"}

    def balance(self):
        self._rec("balance")
        return [
            {"asset": "USDT", "balance": "1000.0", "availableBalance": "950.0"},
            {"asset": "BNB", "balance": "1.0", "availableBalance": "1.0"},
        ]

    def get_position_risk(self, symbol=None):
        self._rec("get_position_risk", symbol=symbol)
        return [r for r in self.position_rows if r["symbol"] == symbol] or self.position_rows

    def exchange_info(self):
        self._rec("exchange_info")
        return _CANNED_EXCHANGE_INFO

    # --- setup surface ---
    def change_margin_type(self, symbol=None, marginType=None):
        self._rec("change_margin_type", symbol=symbol, marginType=marginType)
        return {"code": 200, "msg": "success"}

    def change_leverage(self, symbol=None, leverage=None):
        self._rec("change_leverage", symbol=symbol, leverage=leverage)
        return {"leverage": leverage, "symbol": symbol}

    # --- order surface ---
    def new_order(self, **params):
        self._rec("new_order", **params)
        return dict(self.fill_response)

    def new_algo_order(self, **params):
        self._rec("new_algo_order", **params)
        oid = 9000 + len(self.algo_orders)
        rec = {"orderId": oid, **params}
        self.algo_orders.append(rec)
        return rec

    def cancel_open_orders(self, symbol=None):
        self._rec("cancel_open_orders", symbol=symbol)
        return {"code": 200, "msg": "success"}

    def get_open_algo_orders(self, symbol=None):
        self._rec("get_open_algo_orders", symbol=symbol)
        return [a for a in self.algo_orders if a.get("symbol") == symbol]


@pytest.fixture
def mock():
    return MockFuturesClient()


@pytest.fixture
def ex(mock):
    # testnet=True so we never even consider the live base URL
    return BinanceFuturesClient(client=mock, testnet=True)


# ---------------------------------------------------------------------------
# Decimal rounding — qty DOWN, never up.
# ---------------------------------------------------------------------------

def test_round_qty_down_floors_to_lot_step(ex):
    # 0.0057 with a 0.001 step must floor to 0.005 (NOT round to 0.006)
    assert ex.round_qty_down("0.0057", "0.001") == Decimal("0.005")
    assert ex.round_qty_down("0.0019", "0.001") == Decimal("0.001")
    # exactly on a step stays put
    assert ex.round_qty_down("0.005", "0.001") == Decimal("0.005")
    # below one step -> 0 (can't trade a sub-lot)
    assert ex.round_qty_down("0.0009", "0.001") == Decimal("0")


def test_round_qty_down_never_rounds_up_module_level():
    # The pure function (used everywhere) must never exceed the input.
    for raw in ("0.0011", "0.0099", "1.23456789", "7.7777"):
        out = binance_exchange.round_qty_down(raw, "0.001")
        assert out <= Decimal(raw), f"{raw} rounded UP to {out}"


def test_round_qty_down_bad_input_is_zero(ex):
    assert ex.round_qty_down(None, "0.001") == Decimal("0")
    assert ex.round_qty_down("-5", "0.001") == Decimal("0")
    assert ex.round_qty_down("abc", "0.001") == Decimal("0")


def test_round_price_snaps_to_tick(ex):
    # 60000.07 with tick 0.10 -> 60000.00
    assert ex.round_price("60000.07", "0.10") == Decimal("60000.00")
    assert ex.round_price("60000.15", "0.10") == Decimal("60000.10")


# ---------------------------------------------------------------------------
# exchangeInfo filters are read LIVE, not hardcoded.
# ---------------------------------------------------------------------------

def test_get_exchange_filters_reads_live(ex, mock):
    f = ex.get_exchange_filters("BTCUSDT")
    assert f["lot_step"] == Decimal("0.001")
    assert f["tick_size"] == Decimal("0.10")
    assert f["min_notional"] == Decimal("100")
    assert ("exchange_info", {}) in mock.calls


def test_get_exchange_filters_picks_correct_symbol(ex):
    f = ex.get_exchange_filters("ETHUSDT")
    assert f["lot_step"] == Decimal("0.01")
    assert f["min_notional"] == Decimal("20")


def test_get_exchange_filters_never_raises_on_failure(mock):
    mock.raise_on["exchange_info"] = RuntimeError("network down")
    ex = BinanceFuturesClient(client=mock)
    f = ex.get_exchange_filters("BTCUSDT")
    # zeros == "unknown" so the caller must refuse to size (fail safe)
    assert f["lot_step"] == Decimal("0")
    assert f["min_notional"] == Decimal("0")


# ---------------------------------------------------------------------------
# min-notional respected.
# ---------------------------------------------------------------------------

def test_min_notional_respected(ex):
    # 0.001 BTC @ 60000 = 60 USDT < 100 min-notional -> rejected
    assert ex.meets_min_notional("0.001", "60000", "100") is False
    # 0.002 BTC @ 60000 = 120 USDT >= 100 -> ok
    assert ex.meets_min_notional("0.002", "60000", "100") is True
    # exactly at the notional is allowed
    assert ex.meets_min_notional("0.002", "50000", "100") is True


def test_min_notional_uses_live_filter_end_to_end(ex):
    filt = ex.get_exchange_filters("BTCUSDT")
    qty = ex.round_qty_down("0.0017", filt["lot_step"])  # -> 0.001
    assert qty == Decimal("0.001")
    # at 60000 that's only 60 USDT, below the live 100 min-notional
    assert ex.meets_min_notional(qty, "60000", filt["min_notional"]) is False


# ---------------------------------------------------------------------------
# set_leverage_isolated: ISOLATED + leverage, swallow -4046.
# ---------------------------------------------------------------------------

def test_set_leverage_isolated_sets_both(ex, mock):
    out = ex.set_leverage_isolated("BTCUSDT", 3)
    assert out["ok"] is True
    names = [c[0] for c in mock.calls]
    assert "change_margin_type" in names
    assert "change_leverage" in names
    # margin type must be ISOLATED (never CROSSED for this risk-managed cockpit)
    mt = [c for c in mock.calls if c[0] == "change_margin_type"][0][1]
    assert mt["marginType"] == "ISOLATED"
    lev = [c for c in mock.calls if c[0] == "change_leverage"][0][1]
    assert lev["leverage"] == 3


def test_set_leverage_isolated_swallows_4046_on_margin(ex, mock):
    # "no need to change margin type" — already isolated. Must be treated as OK.
    mock.raise_on["change_margin_type"] = MockClientError(-4046, "No need to change margin type.")
    out = ex.set_leverage_isolated("BTCUSDT", 3)
    assert out["ok"] is True  # swallowed, NOT a failure
    # and we still went on to set leverage
    assert any(c[0] == "change_leverage" for c in mock.calls)


def test_set_leverage_isolated_swallows_4046_on_leverage(ex, mock):
    mock.raise_on["change_leverage"] = MockClientError(-4046, "No need to change leverage.")
    out = ex.set_leverage_isolated("BTCUSDT", 3)
    assert out["ok"] is True


def test_set_leverage_isolated_swallows_4046_from_plain_message(ex, mock):
    # connector variants that don't carry .error_code, only the text
    mock.raise_on["change_margin_type"] = RuntimeError("APIError(code=-4046): No need to change margin type.")
    out = ex.set_leverage_isolated("BTCUSDT", 3)
    assert out["ok"] is True


def test_set_leverage_isolated_surfaces_real_error(ex, mock):
    # a DIFFERENT error (not -4046) must NOT be swallowed
    mock.raise_on["change_margin_type"] = MockClientError(-4047, "margin type cannot be changed with open position")
    out = ex.set_leverage_isolated("BTCUSDT", 3)
    assert out["ok"] is False
    assert "change_margin_type" in out["error"]


# ---------------------------------------------------------------------------
# place_market returns a fill with avg price + real fees.
# ---------------------------------------------------------------------------

def test_place_market_returns_fill(ex, mock):
    fill = ex.place_market("BTCUSDT", "long", "0.005")
    assert fill["ok"] is True
    assert fill["order_id"] == 555
    # avg of (60000*0.003 + 60010*0.002)/0.005 = 60004.0
    assert fill["avg_price"] == pytest.approx(60004.0)
    # real fee = 0.108 + 0.072 = 0.18 (summed from fills[], NOT hardcoded)
    assert fill["fee"] == pytest.approx(0.18)
    assert fill["qty"] == pytest.approx(0.005)
    # it was a MARKET order on the BUY side
    call = [c for c in mock.calls if c[0] == "new_order"][0][1]
    assert call["type"] == "MARKET"
    assert call["side"] == "BUY"


def test_place_market_maps_short_to_sell(ex, mock):
    ex.place_market("BTCUSDT", "short", "0.005")
    call = [c for c in mock.calls if c[0] == "new_order"][0][1]
    assert call["side"] == "SELL"


def test_place_market_rejects_bad_qty(ex):
    out = ex.place_market("BTCUSDT", "long", "0")
    assert out["ok"] is False
    assert "qty" in out["error"]


def test_place_market_surfaces_exchange_error(ex, mock):
    mock.raise_on["new_order"] = MockClientError(-2019, "Margin is insufficient.")
    out = ex.place_market("BTCUSDT", "long", "0.005")
    assert out["ok"] is False
    assert "insufficient" in out["error"].lower()


# ---------------------------------------------------------------------------
# place_algo_stop: ALGO path + closePosition + MARK_PRICE + priceProtect.
# ---------------------------------------------------------------------------

def test_place_algo_stop_uses_algo_path(ex, mock):
    out = ex.place_algo_stop("BTCUSDT", "SELL", "58000")
    assert out["ok"] is True
    # it went through the ALGO endpoint, NOT the legacy new_order (-4120 path)
    assert any(c[0] == "new_algo_order" for c in mock.calls)
    assert not any(c[0] == "new_order" for c in mock.calls)
    params = [c for c in mock.calls if c[0] == "new_algo_order"][0][1]
    assert params["type"] == "STOP_MARKET"
    assert params["closePosition"] == "true"        # flatten the WHOLE position
    assert params["workingType"] == "MARK_PRICE"     # trigger off mark
    assert params["priceProtect"] == "true"          # spoofed-wick protection
    assert params["stopPrice"] == "58000"
    assert params["side"] == "SELL"


def test_place_algo_take_profit_uses_algo_path(ex, mock):
    out = ex.place_algo_take_profit("BTCUSDT", "SELL", "65000")
    assert out["ok"] is True
    params = [c for c in mock.calls if c[0] == "new_algo_order"][0][1]
    assert params["type"] == "TAKE_PROFIT_MARKET"
    assert params["closePosition"] == "true"
    assert params["workingType"] == "MARK_PRICE"
    assert params["priceProtect"] == "true"


def test_place_algo_stop_rejects_bad_price(ex):
    out = ex.place_algo_stop("BTCUSDT", "SELL", "0")
    assert out["ok"] is False
    assert "stop_price" in out["error"]


def test_place_algo_stop_surfaces_error(ex, mock):
    mock.raise_on["new_algo_order"] = MockClientError(-2021, "Order would immediately trigger.")
    out = ex.place_algo_stop("BTCUSDT", "SELL", "58000")
    assert out["ok"] is False


# ---------------------------------------------------------------------------
# market_close is reduceOnly + opposite direction.
# ---------------------------------------------------------------------------

def test_market_close_is_reduce_only_for_long(ex, mock):
    # set an OPEN long position
    mock.position_rows = [{
        "symbol": "BTCUSDT", "positionAmt": "0.005", "entryPrice": "60000",
        "leverage": "3", "unRealizedProfit": "5", "liquidationPrice": "40000",
    }]
    out = ex.market_close("BTCUSDT")
    assert out["ok"] is True
    call = [c for c in mock.calls if c[0] == "new_order"][0][1]
    assert call["reduceOnly"] == "true"   # can NEVER open/increase
    assert call["side"] == "SELL"          # closing a long
    assert call["type"] == "MARKET"
    assert call["quantity"] == "0.005"


def test_market_close_is_reduce_only_for_short(ex, mock):
    mock.position_rows = [{
        "symbol": "BTCUSDT", "positionAmt": "-0.004", "entryPrice": "60000",
        "leverage": "3", "unRealizedProfit": "-2", "liquidationPrice": "80000",
    }]
    out = ex.market_close("BTCUSDT")
    assert out["ok"] is True
    call = [c for c in mock.calls if c[0] == "new_order"][0][1]
    assert call["reduceOnly"] == "true"
    assert call["side"] == "BUY"           # closing a short
    assert call["quantity"] == "0.004"


def test_market_close_when_flat_does_nothing(ex, mock):
    # default position rows are flat (0 amt) -> no order placed
    out = ex.market_close("BTCUSDT")
    assert out["ok"] is True
    assert out.get("flat") is True
    assert not any(c[0] == "new_order" for c in mock.calls)


# ---------------------------------------------------------------------------
# Reads never raise.
# ---------------------------------------------------------------------------

def test_reads_never_raise_on_client_failure(mock):
    for m in ("account", "balance", "get_position_risk", "exchange_info"):
        mock.raise_on[m] = RuntimeError("boom")
    ex = BinanceFuturesClient(client=mock)
    assert ex.get_account() == {}
    assert ex.get_balance() is None
    assert ex.get_position("BTCUSDT") == {}
    assert ex.get_liquidation_price("BTCUSDT") is None
    assert ex.get_exchange_filters("BTCUSDT")["lot_step"] == Decimal("0")
    assert ex.list_open_algo_orders("BTCUSDT") == []


def test_get_balance_and_liq_and_position_happy_path(ex, mock):
    assert ex.get_balance("USDT") == pytest.approx(950.0)
    mock.position_rows = [{
        "symbol": "BTCUSDT", "positionAmt": "0.005", "entryPrice": "60000",
        "leverage": "3", "unRealizedProfit": "5", "liquidationPrice": "40000",
    }]
    pos = ex.get_position("BTCUSDT")
    assert pos["side"] == "long"
    assert pos["qty"] == pytest.approx(0.005)
    assert ex.get_liquidation_price("BTCUSDT") == pytest.approx(40000.0)


def test_list_open_algo_orders_after_placing_stop(ex):
    ex.place_algo_stop("BTCUSDT", "SELL", "58000")
    orders = ex.list_open_algo_orders("BTCUSDT")
    assert len(orders) == 1
    assert orders[0]["type"] == "STOP_MARKET"
    assert orders[0]["close_position"] is True
    assert orders[0]["stop_price"] == pytest.approx(58000.0)


# ---------------------------------------------------------------------------
# WITHDRAWALS-IMPOSSIBLE — the safety guarantee, proved by scanning the class.
# ---------------------------------------------------------------------------

# Any method whose name contains one of these would be capable of moving funds
# off the account. NONE may exist on the wrapper.
_FORBIDDEN_SUBSTRINGS = (
    "withdraw", "transfer", "send", "payout", "remit",
    "internal", "universal", "sapi", "redeem", "convert",
)


def test_no_withdrawal_or_transfer_method_exists():
    """Scan EVERY attribute of BinanceFuturesClient — no money-egress surface."""
    names = [n for n in dir(BinanceFuturesClient) if not n.startswith("__")]
    offenders = [
        n for n in names
        if any(bad in n.lower() for bad in _FORBIDDEN_SUBSTRINGS)
    ]
    assert offenders == [], f"forbidden money-movement methods present: {offenders}"


def test_no_withdrawal_at_module_level():
    """Even free functions in the module must not expose egress."""
    offenders = [
        n for n, obj in vars(binance_exchange).items()
        if callable(obj) and not n.startswith("_")
        and any(bad in n.lower() for bad in _FORBIDDEN_SUBSTRINGS)
    ]
    assert offenders == [], f"forbidden module-level functions: {offenders}"


def test_wrapper_never_calls_a_withdrawal_on_the_client():
    """Belt-and-suspenders: drive the public surface and assert the wrapper never
    invokes any forbidden method NAME on the injected client."""
    recorder = MockFuturesClient()
    ex = BinanceFuturesClient(client=recorder)
    ex.get_account(); ex.get_balance(); ex.get_position("BTCUSDT")
    ex.get_liquidation_price("BTCUSDT"); ex.get_exchange_filters("BTCUSDT")
    ex.set_leverage_isolated("BTCUSDT", 3)
    recorder.position_rows = [{
        "symbol": "BTCUSDT", "positionAmt": "0.005", "entryPrice": "60000",
        "leverage": "3", "unRealizedProfit": "0", "liquidationPrice": "40000",
    }]
    ex.place_market("BTCUSDT", "long", "0.005")
    ex.place_algo_stop("BTCUSDT", "SELL", "58000")
    ex.market_close("BTCUSDT")
    ex.cancel_open_orders("BTCUSDT")
    ex.list_open_algo_orders("BTCUSDT")
    called = {c[0] for c in recorder.calls}
    bad = [c for c in called if any(b in c.lower() for b in _FORBIDDEN_SUBSTRINGS)]
    assert bad == [], f"wrapper called forbidden client methods: {bad}"


def _strip_comments_and_strings(src: str) -> str:
    """Tokenize and drop comments + string literals (docstrings included), so we
    test only the EXECUTABLE code — prose that says 'there is no withdraw method'
    must not trip the egress scan."""
    import io
    import tokenize

    out_tokens = []
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in toks:
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out_tokens.append(tok.string)
    except tokenize.TokenError:
        # fall back to the raw source if tokenizing trips on something
        return src
    return " ".join(out_tokens).lower()


def test_inspect_source_has_no_withdraw_endpoints():
    """The EXECUTABLE source (comments/strings stripped) must not reference any
    withdrawal/transfer endpoint or call. The class/module method scans above
    already prove no egress surface is EXPOSED; this guards against an internal
    egress call hidden inside a method body."""
    code = _strip_comments_and_strings(inspect.getsource(binance_exchange))
    for bad in ("sapi", "withdraw", "transfer", "universal_transfer",
                "futures_transfer", "payout", "redeem"):
        assert bad not in code, f"executable source references forbidden token: {bad!r}"
