"""Unit tests for engine/binance_cockpit.py — the manual-cockpit SAFETY BRAIN.

Every test injects a MockFuturesClient (records calls, returns canned filters /
positions / fills, lets any method be told to raise) wrapped by the REAL
BinanceFuturesClient, so the full production stack (lot rounding, ALGO stop path,
reduceOnly close) is exercised without a single real network call or real key.

It proves the NON-NEGOTIABLE safety guarantees:

  * preview_trade returns sane NET costs + liquidation + an itemised checks list,
    and REJECTS a too-wide stop (leverage cap) and a sub-min-notional order;
  * place_manual_trade routes through risk_engine.gate_order and a REJECTED gate
    places NO order at all;
  * FAULT INJECTION: when the stop cannot be verified live after entry,
    place_manual_trade MARKET-CLOSES the position (no naked risk), records a
    fault, and raises NakedPositionError;
  * reconcile_on_start FLATTENS a position that has no live stop;
  * a LOSS SEQUENCE never increases size (no martingale) — sizing falls with
    equity, never rises.
"""
from __future__ import annotations

import importlib
from decimal import Decimal

import pytest

import binance_cockpit
import risk_engine
from binance_cockpit import NakedPositionError
from binance_exchange import BinanceFuturesClient


# ---------------------------------------------------------------------------
# Mock connector — same surface the real connector exposes.
# ---------------------------------------------------------------------------

class MockClientError(Exception):
    def __init__(self, error_code: int, msg: str = ""):
        super().__init__(msg or f"binance error {error_code}")
        self.error_code = error_code


_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
            ],
        },
    ]
}


class MockFuturesClient:
    """Records calls; returns canned data; any method can be told to raise.

    `stop_goes_live` controls whether a placed STOP_MARKET algo actually appears
    in get_open_algo_orders — this is the FAULT-INJECTION lever for the
    naked-position guard."""

    def __init__(self, *, liq_price="40000", stop_goes_live=True):
        self.calls: list[tuple[str, dict]] = []
        self.raise_on: dict[str, Exception] = {}
        self.stop_goes_live = stop_goes_live
        self.position_rows = [{
            "symbol": "BTCUSDT", "positionAmt": "0.0", "entryPrice": "0.0",
            "leverage": "3", "unRealizedProfit": "0.0", "liquidationPrice": str(liq_price),
        }]
        self.fill_response = {
            "orderId": 555, "status": "FILLED", "executedQty": "0.005", "avgPrice": "0",
            "fills": [{"price": "60000.0", "qty": "0.005", "commission": "0.15"}],
        }
        self.algo_orders: list[dict] = []

    def _rec(self, name, **kw):
        self.calls.append((name, kw))
        if name in self.raise_on:
            raise self.raise_on[name]

    def names(self):
        return [c[0] for c in self.calls]

    # read surface
    def account(self):
        self._rec("account")
        return {"availableBalance": "1000.0"}

    def balance(self):
        self._rec("balance")
        return [{"asset": "USDT", "balance": "1000.0", "availableBalance": "1000.0"}]

    def get_position_risk(self, symbol=None):
        self._rec("get_position_risk", symbol=symbol)
        return [r for r in self.position_rows if r["symbol"] == symbol] or self.position_rows

    def exchange_info(self):
        self._rec("exchange_info")
        return _EXCHANGE_INFO

    # setup surface
    def change_margin_type(self, symbol=None, marginType=None):
        self._rec("change_margin_type", symbol=symbol, marginType=marginType)
        return {"code": 200}

    def change_leverage(self, symbol=None, leverage=None):
        self._rec("change_leverage", symbol=symbol, leverage=leverage)
        return {"leverage": leverage}

    # order surface
    def new_order(self, **params):
        self._rec("new_order", **params)
        sym = params.get("symbol")
        if str(params.get("reduceOnly")).lower() == "true":
            # a reduceOnly market close flattens our reported position
            self.position_rows = [{
                "symbol": sym, "positionAmt": "0.0", "entryPrice": "0.0",
                "leverage": "3", "unRealizedProfit": "0.0", "liquidationPrice": "0",
            }]
            return {"orderId": 777, "status": "FILLED", "executedQty": params.get("quantity"),
                    "avgPrice": "60000", "fills": [{"price": "60000.0",
                    "qty": params.get("quantity"), "commission": "0.15"}]}
        # a plain MARKET entry OPENS the position (so a later market_close has
        # something to flatten — this is what makes the naked-risk guard testable)
        amt = str(params.get("quantity"))
        if str(params.get("side")).upper() == "SELL":
            amt = "-" + amt
        self.position_rows = [{
            "symbol": sym, "positionAmt": amt, "entryPrice": "60000",
            "leverage": "3", "unRealizedProfit": "0", "liquidationPrice": "40000",
        }]
        return dict(self.fill_response)

    def new_algo_order(self, **params):
        self._rec("new_algo_order", **params)
        oid = 9000 + len(self.algo_orders)
        rec = {"orderId": oid, **params}
        # FAULT INJECTION: a STOP that "doesn't go live" is accepted by the API
        # call but never shows up in get_open_algo_orders (rejected async / wick).
        if not (str(params.get("type")).upper().find("STOP") >= 0 and not self.stop_goes_live):
            self.algo_orders.append(rec)
        return rec

    def cancel_open_orders(self, symbol=None):
        self._rec("cancel_open_orders", symbol=symbol)
        self.algo_orders = [a for a in self.algo_orders if a.get("symbol") != symbol]
        return {"code": 200}

    def get_open_algo_orders(self, symbol=None):
        self._rec("get_open_algo_orders", symbol=symbol)
        return [a for a in self.algo_orders if a.get("symbol") == symbol]

    # helper to seed an OPEN position for reconcile / close tests
    def set_open_long(self, symbol="BTCUSDT", qty="0.005", entry="60000", liq="40000"):
        self.position_rows = [{
            "symbol": symbol, "positionAmt": qty, "entryPrice": entry,
            "leverage": "3", "unRealizedProfit": "0", "liquidationPrice": liq,
        }]


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point the sidecar binance_audit.db at a temp dir so tests never touch the
    real DATA_ROOT and never collide. Reset the cached connection per test."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    binance_cockpit._conn = None
    binance_cockpit._conn_path = None
    yield
    binance_cockpit._conn = None
    binance_cockpit._conn_path = None


@pytest.fixture
def mock():
    return MockFuturesClient()


@pytest.fixture
def ex(mock):
    return BinanceFuturesClient(client=mock, testnet=True)


# a clean, in-caps risk state
GOOD_RS = {"day_pnl_pct": 0.0, "peak_drawdown_pct": 0.0}


# ===========================================================================
# preview_trade — transparency: costs, liquidation, checks, approval.
# ===========================================================================

def test_preview_returns_sane_costs_liquidation_and_checks(ex):
    # long 60000, stop 57000 (5% away), target 66000 (R:R 2:1), 2% risk on 10k equity
    pv = ex_preview(ex, side="long", entry=60000, stop=57000, target=66000,
                    equity=10000, risk_pct=2.0, leverage=3)
    assert pv["approved"] is True
    assert pv["qty"] > 0
    # NET costs: round-trip taker (0.05%*2) + slippage (0.02%*2) on the notional
    expected_fee = pv["notional"] * 0.0005 * 2
    expected_slip = pv["notional"] * 0.0002 * 2
    assert pv["fee_est"] == pytest.approx(expected_fee, rel=1e-6)
    assert pv["slippage_est"] == pytest.approx(expected_slip, rel=1e-6)
    assert pv["total_cost"] == pytest.approx(expected_fee + expected_slip, rel=1e-6)
    # liquidation pulled from positionRisk
    assert pv["liquidation_price"] == pytest.approx(40000.0)
    # NET outcomes subtract costs from the gross move
    gross_win = (66000 - 60000) * pv["qty"]
    assert pv["net_target"] == pytest.approx(gross_win - pv["total_cost"], rel=1e-6)
    gross_loss = (57000 - 60000) * pv["qty"]
    assert pv["net_if_stopped"] == pytest.approx(gross_loss - pv["total_cost"], rel=1e-6)
    # an itemised checks list, all passing on this sane order
    names = {c["name"] for c in pv["checks"]}
    assert {"inputs", "stop_direction", "risk_gate", "lot_step", "min_notional"} <= names
    assert all(c["ok"] for c in pv["checks"]), pv["checks"]


def test_preview_rejects_too_wide_stop_via_leverage_cap(ex):
    # A very TIGHT stop forces effective leverage above the 3x cap at 2% risk.
    # (qty = risk$/stop_dist; tiny stop_dist -> huge qty -> huge leverage.)
    pv = ex_preview(ex, side="long", entry=60000, stop=59990, target=60030,
                    equity=10000, risk_pct=2.0, leverage=3)
    assert pv["approved"] is False
    gate = [c for c in pv["checks"] if c["name"] == "risk_gate"][0]
    assert gate["ok"] is False
    assert "leverage" in gate["reason"].lower()


def test_preview_rejects_wrong_side_stop(ex):
    # a long with a stop ABOVE entry does not protect -> rejected
    pv = ex_preview(ex, side="long", entry=60000, stop=61000, target=66000,
                    equity=10000, risk_pct=1.0)
    assert pv["approved"] is False
    sd = [c for c in pv["checks"] if c["name"] == "stop_direction"][0]
    assert sd["ok"] is False


def test_preview_rejects_sub_min_notional(ex):
    # Tiny equity + tiny risk -> sized qty rounds to ~0.001 BTC = 60 USDT < 100 min.
    pv = ex_preview(ex, side="long", entry=60000, stop=57000, target=66000,
                    equity=200, risk_pct=0.5, leverage=3)
    mn = [c for c in pv["checks"] if c["name"] == "min_notional"][0]
    assert mn["ok"] is False
    assert pv["approved"] is False


def test_preview_never_raises_on_garbage(ex):
    pv = ex_preview(ex, side="banana", entry="x", stop=None, target=None,
                    equity=-5, risk_pct=2.0)
    assert pv["approved"] is False
    assert isinstance(pv["checks"], list) and pv["checks"]


# ===========================================================================
# place_manual_trade — gate is the ONLY path; a reject places NO order.
# ===========================================================================

def test_place_routes_through_gate_and_places_protected_trade(ex, mock):
    res = binance_cockpit.place_manual_trade(ex, {
        "symbol": "BTCUSDT", "side": "long", "entry": 60000, "stop": 57000,
        "target": 66000, "equity": 10000, "risk_pct": 2.0, "leverage": 3,
    }, GOOD_RS)
    assert res["ok"] is True
    assert res["stop_verified"] is True
    names = mock.names()
    # order of operations: leverage -> entry -> stop -> verify
    assert "change_leverage" in names
    assert "new_order" in names          # market entry
    assert "new_algo_order" in names     # the stop (and TP)
    assert "get_open_algo_orders" in names  # verification
    # a STOP_MARKET algo with closePosition was placed
    algo = [c[1] for c in mock.calls if c[0] == "new_algo_order"]
    assert any(p["type"] == "STOP_MARKET" and p["closePosition"] == "true" for p in algo)


def test_rejected_gate_places_NO_order(ex, mock):
    # Daily cap already breached -> gate must reject and NOTHING may be ordered.
    res = binance_cockpit.place_manual_trade(ex, {
        "symbol": "BTCUSDT", "side": "long", "entry": 60000, "stop": 57000,
        "target": 66000, "equity": 10000, "risk_pct": 2.0,
    }, {"day_pnl_pct": -5.0, "peak_drawdown_pct": -5.0})
    assert res["ok"] is False
    assert res["placed_order"] is False
    # the single most important assertion: NO entry, NO stop, NOTHING placed.
    assert "new_order" not in mock.names()
    assert "new_algo_order" not in mock.names()
    assert "change_leverage" not in mock.names()


def test_rejected_gate_on_bad_rr_places_no_order(ex, mock):
    # R:R below 2:1 -> gate rejects (sizing rejection) -> no order
    res = binance_cockpit.place_manual_trade(ex, {
        "symbol": "BTCUSDT", "side": "long", "entry": 60000, "stop": 57000,
        "target": 61000, "equity": 10000, "risk_pct": 2.0,  # reward 1000 < 2*3000
    }, GOOD_RS)
    assert res["ok"] is False
    assert res["placed_order"] is False
    assert "new_order" not in mock.names()


# ===========================================================================
# FAULT INJECTION — stop not live after entry -> market-close + fault + raise.
# ===========================================================================

def test_fault_injection_unverified_stop_flattens_and_raises(ex, mock):
    # the stop call "succeeds" but the order never goes live (async reject / wick)
    mock.stop_goes_live = False
    with pytest.raises(NakedPositionError):
        binance_cockpit.place_manual_trade(ex, {
            "symbol": "BTCUSDT", "side": "long", "entry": 60000, "stop": 57000,
            "target": 66000, "equity": 10000, "risk_pct": 2.0,
        }, GOOD_RS)
    # the entry WAS placed (so we really had naked exposure for an instant)...
    entry_calls = [c for c in mock.calls if c[0] == "new_order"
                   and str(c[1].get("reduceOnly")).lower() != "true"]
    assert entry_calls, "entry should have been attempted"
    # ...and then it was MARKET-CLOSED with reduceOnly (no naked risk left)
    close_calls = [c for c in mock.calls if c[0] == "new_order"
                   and str(c[1].get("reduceOnly")).lower() == "true"]
    assert close_calls, "position must be market-closed when the stop can't be verified"
    # a fault was recorded to the sidecar DB
    faults = [t for t in binance_cockpit.list_trades() if t["event"] == "fault"]
    assert faults, "a loud fault must be recorded on the naked-position guard"


def test_fault_injection_stop_api_error_also_flattens(ex, mock):
    # this time the stop ALGO call itself raises -> still must flatten + raise
    mock.raise_on["new_algo_order"] = MockClientError(-2021, "Order would immediately trigger.")
    with pytest.raises(NakedPositionError):
        binance_cockpit.place_manual_trade(ex, {
            "symbol": "BTCUSDT", "side": "long", "entry": 60000, "stop": 57000,
            "target": 66000, "equity": 10000, "risk_pct": 2.0,
        }, GOOD_RS)
    close_calls = [c for c in mock.calls if c[0] == "new_order"
                   and str(c[1].get("reduceOnly")).lower() == "true"]
    assert close_calls, "a failed stop must trigger an auto-flatten"


# ===========================================================================
# reconcile_on_start — flatten a position that has NO live stop.
# ===========================================================================

def test_reconcile_protects_naked_position_by_placing_stop(ex, mock):
    mock.set_open_long(qty="0.005", entry="60000", liq="40000")
    # no algo orders exist -> position is naked
    report = binance_cockpit.reconcile_on_start(ex, ["BTCUSDT"])
    assert "BTCUSDT" in report["protected"]
    # a STOP_MARKET algo is now live
    stops = [a for a in mock.algo_orders if a["type"] == "STOP_MARKET"]
    assert stops, "reconcile should have placed a protective stop"


def test_reconcile_flattens_when_stop_cannot_be_placed(ex, mock):
    mock.set_open_long(qty="0.005", entry="60000", liq="40000")
    mock.stop_goes_live = False  # any stop we place will NOT go live
    report = binance_cockpit.reconcile_on_start(ex, ["BTCUSDT"])
    assert "BTCUSDT" in report["flattened"]
    assert "BTCUSDT" not in report["protected"]
    # it was closed reduceOnly
    close_calls = [c for c in mock.calls if c[0] == "new_order"
                   and str(c[1].get("reduceOnly")).lower() == "true"]
    assert close_calls, "a position that cannot be protected must be flattened"


def test_reconcile_leaves_already_protected_position_alone(ex, mock):
    mock.set_open_long(qty="0.005", entry="60000", liq="40000")
    # seed an already-live stop
    ex.place_algo_stop("BTCUSDT", "SELL", "57000")
    n_orders_before = len([c for c in mock.calls if c[0] == "new_order"])
    report = binance_cockpit.reconcile_on_start(ex, ["BTCUSDT"])
    assert "BTCUSDT" in report["ok"]
    assert "BTCUSDT" not in report["flattened"]
    # no flatten order was sent
    n_orders_after = len([c for c in mock.calls if c[0] == "new_order"])
    assert n_orders_after == n_orders_before


def test_reconcile_ignores_flat_symbols(ex, mock):
    # default position is flat
    report = binance_cockpit.reconcile_on_start(ex, ["BTCUSDT"])
    assert report["protected"] == [] and report["flattened"] == []


# ===========================================================================
# close_position — flatten + cancel + log exit.
# ===========================================================================

def test_close_position_flattens_and_cancels(ex, mock):
    mock.set_open_long(qty="0.005", entry="60000", liq="40000")
    ex.place_algo_stop("BTCUSDT", "SELL", "57000")
    res = binance_cockpit.close_position(ex, "BTCUSDT")
    assert res["ok"] is True
    assert "cancel_open_orders" in mock.names()
    close_calls = [c for c in mock.calls if c[0] == "new_order"
                   and str(c[1].get("reduceOnly")).lower() == "true"]
    assert close_calls
    exits = [t for t in binance_cockpit.list_trades() if t["event"] == "exit"]
    assert exits, "an exit row must be logged"


# ===========================================================================
# enforce_caps — breach flattens everything + blocks.
# ===========================================================================

def test_enforce_caps_within_limits_allows(ex):
    out = binance_cockpit.enforce_caps(ex, GOOD_RS, symbols=["BTCUSDT"])
    assert out["allow_new"] is True
    assert out["flatten"] is False
    assert out["closed"] == []


def test_enforce_caps_daily_breach_flattens_and_blocks(ex, mock):
    mock.set_open_long(qty="0.005", entry="60000", liq="40000")
    out = binance_cockpit.enforce_caps(
        ex, {"day_pnl_pct": -4.0, "peak_drawdown_pct": -4.0}, symbols=["BTCUSDT"])
    assert out["allow_new"] is False
    assert out["flatten"] is True
    assert out["closed"] and out["closed"][0]["symbol"] == "BTCUSDT"
    close_calls = [c for c in mock.calls if c[0] == "new_order"
                   and str(c[1].get("reduceOnly")).lower() == "true"]
    assert close_calls, "a cap breach must flatten open positions"


def test_enforce_caps_global_breach_halts(ex):
    out = binance_cockpit.enforce_caps(
        ex, {"day_pnl_pct": -2.0, "peak_drawdown_pct": -12.0}, symbols=["BTCUSDT"])
    assert out["halt"] is True
    assert out["allow_new"] is False


# ===========================================================================
# NO MARTINGALE — a loss sequence never increases size.
# ===========================================================================

def test_loss_sequence_never_increases_size(ex):
    """After each loss the account equity falls; the cockpit's sized qty must
    fall too — it must NEVER grow (no doubling / averaging / recovery). We also
    pass a deliberately HOSTILE risk_state carrying loss-streak fields to prove
    they are ignored (there is no input that can size us up)."""
    equities = [10000, 9700, 9400, 9100]  # equity shrinking after losses
    last_qty = None
    for i, eq in enumerate(equities):
        hostile_rs = {
            "day_pnl_pct": 0.0, "peak_drawdown_pct": 0.0,
            # fields a martingale would (wrongly) use to scale up:
            "loss_streak": i, "last_loss": -300.0, "recovery_multiplier": 2 ** i,
        }
        pv = ex_preview(ex, side="long", entry=60000, stop=57000, target=66000,
                        equity=eq, risk_pct=2.0, leverage=3, risk_state=hostile_rs)
        assert pv["approved"] is True
        if last_qty is not None:
            assert pv["qty"] <= last_qty, (
                f"size INCREASED after a loss ({last_qty} -> {pv['qty']}) — martingale leak!")
        last_qty = pv["qty"]


def test_gate_is_the_only_sizing_path_proof(ex):
    """The cockpit's qty must equal what risk_engine.gate_order returns (snapped
    DOWN to the lot step) — proving sizing has exactly one source of truth."""
    eq, entry, stop, target = 10000, 60000, 57000, 66000
    pv = ex_preview(ex, side="long", entry=entry, stop=stop, target=target,
                    equity=eq, risk_pct=2.0, leverage=3)
    gate = risk_engine.gate_order(
        {"signal": "long", "entry": entry, "stop": stop, "target": target},
        eq, GOOD_RS, {"risk_pct": 2.0})
    # gate qty rounded DOWN to the 0.001 lot step
    from binance_exchange import round_qty_down
    expected = float(round_qty_down(gate["qty"], "0.001"))
    assert pv["qty"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# tiny wrapper so the preview calls read cleanly above
# ---------------------------------------------------------------------------

def ex_preview(ex, *, side, entry, stop, target=None, equity, risk_pct,
               leverage=3, risk_state=None):
    return binance_cockpit.preview_trade(
        ex, symbol="BTCUSDT", side=side, entry=entry, stop=stop, target=target,
        equity=equity, risk_pct=risk_pct, leverage=leverage,
        risk_state=risk_state or GOOD_RS,
    )


def test_module_reimports_cleanly():
    # bare-import sanity (the project runs tests with a bare import)
    importlib.reload(binance_cockpit)
