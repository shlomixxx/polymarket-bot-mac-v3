"""
binance_exchange.py — a thin, testable wrapper over the Binance USDⓈ-M Futures
REST API for the RESPONSIBLE MANUAL-TRADING COCKPIT.

This module is the ONLY place that talks to the exchange. It is deliberately
small, dumb, and auditable. It does NOT decide whether to trade, how big, or
whether an order is safe — that is the job of risk_engine.gate_order (the single
approve path) and the cockpit orchestration layer. This file only:

  * reads account / position / liquidation / exchangeInfo state (READ PATH:
    never raises — returns a safe default + logs);
  * rounds qty DOWN to the lot step and price to the tick (Decimal, never float
    drift);
  * sets ISOLATED margin + leverage at startup (swallows -4046 "no need");
  * places a MARKET entry, and stop / take-profit via the ALGO ORDER API
    (closePosition=true + workingType=MARK_PRICE + priceProtect=true), the only
    way stops still work since the legacy /fapi/v1/order stop path began
    rejecting with -4120 on 2025-12-09;
  * flattens a position (reduceOnly / closePosition) and lists / cancels open
    orders.

NON-NEGOTIABLE — by construction this module CANNOT move money off the account:
  there is NO withdraw / transfer / universal-transfer / internal-transfer
  method here, AT ALL. test_binance_exchange.py scans the class to prove it.

The `client` is INJECTABLE. In production it is built from secret_store keys
(only when live), and is the official `binance-futures-connector` UMFutures
object. In tests it is a MockFuturesClient that records calls and returns canned
exchangeInfo / positions / fills — so unit tests touch NO real network and NO
real keys. We only depend on this duck-typed surface of the client:

    account(), balance(), get_position_risk(symbol=...),
    exchange_info(), change_margin_type(...), change_leverage(...),
    new_order(**params), new_algo_order(**params),
    cancel_open_orders(symbol=...), get_open_algo_orders(symbol=...)

Every order method surfaces errors CLEARLY (returns {"ok": False, "error": ...}
and never silently "succeeds"); read methods never raise.
"""
from __future__ import annotations

import logging
import os
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Optional

_log = logging.getLogger(__name__)

# secret_store SERVICE for the Binance futures keys (cloned pattern, distinct
# from the polymarket one so the two key stores never collide).
SECRET_SERVICE = "binance-futures-bot"

# Binance "no need to change margin type / leverage" — NOT an error for us.
_BINANCE_NO_NEED_CODE = -4046


# ---------------------------------------------------------------------------
# Decimal helpers — qty/price rounding must never use binary float.
# ---------------------------------------------------------------------------

def _D(x: Any) -> Optional[Decimal]:
    """Coerce to Decimal via str (avoids float binary error). None if impossible."""
    if x is None:
        return None
    try:
        if isinstance(x, Decimal):
            d = x
        else:
            d = Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


def round_qty_down(qty: Any, step: Any) -> Decimal:
    """Round qty DOWN to a multiple of the lot step (never round up — would
    risk over-sizing past what risk_engine approved). Returns Decimal("0") on
    bad input. Pure, never raises."""
    q = _D(qty)
    s = _D(step)
    if q is None or q <= 0:
        return Decimal("0")
    if s is None or s <= 0:
        # No/zero step -> nothing to snap to; return the value floored to 0 if neg.
        return q if q > 0 else Decimal("0")
    # floor(q / s) * s, using Decimal quantize semantics.
    steps = (q / s).to_integral_value(rounding=ROUND_DOWN)
    out = steps * s
    # Normalize to the step's exponent so we don't emit 1.000000000.
    return out.quantize(s) if s.as_tuple().exponent < 0 else out


def round_price(price: Any, tick: Any) -> Decimal:
    """Round price to the nearest valid tick (DOWN — conservative for both sides:
    we never invent a better price than the grid allows). Decimal, never raises."""
    p = _D(price)
    t = _D(tick)
    if p is None:
        return Decimal("0")
    if t is None or t <= 0:
        return p
    steps = (p / t).to_integral_value(rounding=ROUND_DOWN)
    out = steps * t
    return out.quantize(t) if t.as_tuple().exponent < 0 else out


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------

class BinanceFuturesClient:
    """Thin USDⓈ-M Futures wrapper. `client` is injectable for tests."""

    def __init__(self, client: Any = None, *, testnet: bool = True) -> None:
        self.testnet = bool(testnet)
        self._client = client if client is not None else self._build_real_client()
        # tiny per-symbol exchangeInfo cache (filters don't change intraday)
        self._filters_cache: dict[str, dict[str, Decimal]] = {}

    # -- real client construction (only reached when no client is injected) --
    def _build_real_client(self) -> Any:
        """Build the official connector from secret_store keys. Only happens in
        production (no client injected). Imported lazily so tests never need the
        library installed, and keys are NEVER logged."""
        try:
            from binance.um_futures import UMFutures  # type: ignore
        except Exception as exc:  # pragma: no cover - prod-only path
            raise RuntimeError(
                "binance-futures-connector not installed; inject a client in tests"
            ) from exc
        import secret_store  # local import; clone of the keyring pattern

        # Keys are stored as "KEY\nSECRET" or via dedicated env; never printed.
        api_key = os.environ.get("BINANCE_API_KEY")
        api_secret = os.environ.get("BINANCE_API_SECRET")
        if not (api_key and api_secret):
            blob = None
            try:
                # MUST be scoped to the Binance service so we never read the
                # Polymarket key store (the two stores must never collide).
                blob = secret_store.load_key(service=SECRET_SERVICE)
            except TypeError:
                # Older secret_store without a `service` param — refuse rather
                # than silently fall back to the polymarket-scoped key.
                _log.error(
                    "secret_store.load_key() has no `service` param; cannot scope "
                    "to %s — set BINANCE_API_KEY/SECRET env instead", SECRET_SERVICE,
                )
                blob = None
            except Exception:
                blob = None
            if blob and "\n" in blob:
                api_key, api_secret = blob.split("\n", 1)
        base_url = (
            "https://testnet.binancefuture.com"
            if self.testnet
            else "https://fapi.binance.com"
        )
        return UMFutures(key=api_key, secret=api_secret, base_url=base_url)

    # ------------------------------------------------------------------ READ
    # Read path: NEVER raises. On any failure -> a safe default + a log line.

    def get_account(self) -> dict[str, Any]:
        try:
            return dict(self._client.account() or {})
        except Exception as exc:
            _log.warning("get_account failed: %s", exc)
            return {}

    def get_balance(self, asset: str = "USDT") -> Optional[float]:
        """Available balance for `asset` (USDT by default). None on failure."""
        try:
            data = self._client.balance()
            for row in data or []:
                if (row.get("asset") or "").upper() == asset.upper():
                    # availableBalance is what we can actually open against.
                    val = row.get("availableBalance", row.get("balance"))
                    f = _D(val)
                    return float(f) if f is not None else None
            return None
        except Exception as exc:
            _log.warning("get_balance failed: %s", exc)
            return None

    def get_position(self, symbol: str) -> dict[str, Any]:
        """Current position for `symbol`. Empty dict if flat / on failure.

        Normalizes the connector row to: {symbol, qty (signed), entry_price,
        side ('long'/'short'/'flat'), leverage, unrealized_pnl, raw}.
        """
        try:
            rows = self._client.get_position_risk(symbol=symbol) or []
            for row in rows:
                if (row.get("symbol") or "") != symbol:
                    continue
                amt = _D(row.get("positionAmt"))
                amt_f = float(amt) if amt is not None else 0.0
                side = "flat"
                if amt_f > 0:
                    side = "long"
                elif amt_f < 0:
                    side = "short"
                return {
                    "symbol": symbol,
                    "qty": amt_f,
                    "entry_price": float(_D(row.get("entryPrice")) or 0),
                    "side": side,
                    "leverage": float(_D(row.get("leverage")) or 0),
                    "unrealized_pnl": float(_D(row.get("unRealizedProfit")) or 0),
                    "raw": row,
                }
            return {}
        except Exception as exc:
            _log.warning("get_position failed for %s: %s", symbol, exc)
            return {}

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Every CURRENTLY-OPEN position across the whole futures account.

        Used by reconcile_on_start (and enforce_caps) to discover naked positions
        WITHOUT being told which symbols to look at — so a position opened outside
        the cockpit still gets a stop or gets flattened on boot. Normalizes each
        row to the same shape get_position returns; only non-flat rows are kept.
        NEVER raises — empty list on failure (read path)."""
        try:
            rows = self._client.get_position_risk() or []
            out: list[dict[str, Any]] = []
            for row in rows:
                amt = _D(row.get("positionAmt"))
                amt_f = float(amt) if amt is not None else 0.0
                if amt_f == 0.0:
                    continue  # flat — skip
                side = "long" if amt_f > 0 else "short"
                out.append({
                    "symbol": row.get("symbol") or "",
                    "qty": amt_f,
                    "entry_price": float(_D(row.get("entryPrice")) or 0),
                    "side": side,
                    "leverage": float(_D(row.get("leverage")) or 0),
                    "unrealized_pnl": float(_D(row.get("unRealizedProfit")) or 0),
                    "raw": row,
                })
            return out
        except Exception as exc:
            _log.warning("get_open_positions failed: %s", exc)
            return []

    def get_liquidation_price(self, symbol: str) -> Optional[float]:
        """Liquidation price from positionRisk. None if flat / unavailable."""
        try:
            rows = self._client.get_position_risk(symbol=symbol) or []
            for row in rows:
                if (row.get("symbol") or "") != symbol:
                    continue
                liq = _D(row.get("liquidationPrice"))
                if liq is None or liq <= 0:
                    return None
                return float(liq)
            return None
        except Exception as exc:
            _log.warning("get_liquidation_price failed for %s: %s", symbol, exc)
            return None

    def get_exchange_filters(self, symbol: str) -> dict[str, Decimal]:
        """Live {lot_step, tick_size, min_notional} from exchangeInfo. NEVER
        hardcoded. Returns zeros on failure (callers must treat 0 as 'unknown'
        and refuse to size). Cached per symbol."""
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        out = {
            "lot_step": Decimal("0"),
            "tick_size": Decimal("0"),
            "min_notional": Decimal("0"),
        }
        try:
            info = self._client.exchange_info() or {}
            for sym in info.get("symbols", []):
                if (sym.get("symbol") or "") != symbol:
                    continue
                for filt in sym.get("filters", []):
                    ftype = filt.get("filterType")
                    if ftype == "LOT_SIZE":
                        v = _D(filt.get("stepSize"))
                        if v is not None:
                            out["lot_step"] = v
                    elif ftype == "PRICE_FILTER":
                        v = _D(filt.get("tickSize"))
                        if v is not None:
                            out["tick_size"] = v
                    elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                        v = _D(filt.get("notional") or filt.get("minNotional"))
                        if v is not None:
                            out["min_notional"] = v
                break
            self._filters_cache[symbol] = out
            return out
        except Exception as exc:
            _log.warning("get_exchange_filters failed for %s: %s", symbol, exc)
            return out

    # passthrough Decimal helpers as methods (per the task's API surface)
    def round_qty_down(self, qty: Any, step: Any) -> Decimal:
        return round_qty_down(qty, step)

    def round_price(self, price: Any, tick: Any) -> Decimal:
        return round_price(price, tick)

    def meets_min_notional(self, qty: Any, price: Any, min_notional: Any) -> bool:
        """True iff qty*price >= min_notional (exchange would reject otherwise)."""
        q = _D(qty)
        p = _D(price)
        mn = _D(min_notional)
        if q is None or p is None or q <= 0 or p <= 0:
            return False
        if mn is None or mn <= 0:
            return True  # no constraint known
        return (q * p) >= mn

    # ----------------------------------------------------------------- SETUP

    def set_leverage_isolated(self, symbol: str, leverage: int) -> dict[str, Any]:
        """Set ISOLATED margin + the given leverage. Swallows Binance -4046
        ("no need to change"), which is success for us. Returns {ok, ...}."""
        result: dict[str, Any] = {"ok": True, "symbol": symbol,
                                  "leverage": int(leverage), "margin_type": "ISOLATED"}
        # 1) margin type ISOLATED (idempotent; -4046 == already isolated)
        try:
            self._client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except Exception as exc:
            if not _is_no_need_error(exc):
                _log.error("set_margin_type ISOLATED failed for %s: %s", symbol, exc)
                return {"ok": False, "symbol": symbol,
                        "error": f"change_margin_type: {exc}"}
        # 2) leverage (idempotent; -4046 == already that leverage)
        try:
            self._client.change_leverage(symbol=symbol, leverage=int(leverage))
        except Exception as exc:
            if not _is_no_need_error(exc):
                _log.error("change_leverage failed for %s: %s", symbol, exc)
                return {"ok": False, "symbol": symbol,
                        "error": f"change_leverage: {exc}"}
        return result

    # ----------------------------------------------------------------- ORDERS
    # Order path: surfaces errors CLEARLY. Never pretends a failed order worked.

    def place_market(self, symbol: str, side: str, qty: Any) -> dict[str, Any]:
        """Place a MARKET entry. Returns a fill:
        {ok, order_id, avg_price, fee, qty, side, raw} or {ok:False, error}.

        NOTE: this method does NOT decide size — risk_engine.gate_order already
        approved `qty`. It only rounds is left to the caller via round_qty_down.
        """
        s = _norm_side(side)
        if s is None:
            return {"ok": False, "error": f"invalid side {side!r} (need BUY/SELL)"}
        q = _D(qty)
        if q is None or q <= 0:
            return {"ok": False, "error": f"invalid qty {qty!r} (must be > 0)"}
        try:
            resp = self._client.new_order(
                symbol=symbol, side=s, type="MARKET", quantity=str(q)
            )
        except Exception as exc:
            _log.error("place_market failed for %s %s %s: %s", symbol, s, q, exc)
            return {"ok": False, "error": str(exc), "symbol": symbol, "side": s}
        return _parse_fill(resp, symbol=symbol, side=s)

    def place_algo_stop(self, symbol: str, side: str, stop_price: Any) -> dict[str, Any]:
        """Place the STOP-LOSS via the ALGO ORDER API — the ONLY path that still
        works (legacy /fapi/v1/order stop rejects -4120 since 2025-12-09).

        Uses closePosition=true + workingType=MARK_PRICE + priceProtect=true so
        the whole position is flattened on a MARK-price trigger (no naked
        leveraged risk left behind). `side` is the CLOSING side (opposite the
        position): a long is stopped with a SELL stop, a short with a BUY stop.
        """
        return self._place_algo(symbol, side, stop_price, kind="STOP_MARKET")

    def place_algo_take_profit(self, symbol: str, side: str,
                               stop_price: Any) -> dict[str, Any]:
        """Take-profit via the same ALGO path (TAKE_PROFIT_MARKET, closePosition,
        MARK_PRICE, priceProtect). Optional / discretionary — the STOP is the
        mandatory one, never the TP."""
        return self._place_algo(symbol, side, stop_price, kind="TAKE_PROFIT_MARKET")

    def _place_algo(self, symbol: str, side: str, stop_price: Any,
                    *, kind: str) -> dict[str, Any]:
        s = _norm_side(side)
        if s is None:
            return {"ok": False, "error": f"invalid side {side!r} (need BUY/SELL)"}
        sp = _D(stop_price)
        if sp is None or sp <= 0:
            return {"ok": False, "error": f"invalid stop_price {stop_price!r}"}
        params = {
            "symbol": symbol,
            "side": s,
            "type": kind,
            "stopPrice": str(sp),
            "closePosition": "true",      # flatten the WHOLE position
            "workingType": "MARK_PRICE",  # trigger off mark, not last
            "priceProtect": "true",       # protect against spoofed wicks
        }
        try:
            resp = self._client.new_algo_order(**params)
        except Exception as exc:
            _log.error("%s algo failed for %s %s @ %s: %s",
                       kind, symbol, s, sp, exc)
            return {"ok": False, "error": str(exc), "symbol": symbol,
                    "side": s, "kind": kind}
        return {
            "ok": True,
            "order_id": resp.get("orderId") or resp.get("algoId")
            if isinstance(resp, dict) else None,
            "symbol": symbol,
            "side": s,
            "kind": kind,
            "stop_price": float(sp),
            "close_position": True,
            "working_type": "MARK_PRICE",
            "price_protect": True,
            "raw": resp,
        }

    def cancel_open_orders(self, symbol: str) -> dict[str, Any]:
        """Cancel ALL open orders for `symbol` (both regular and algo)."""
        try:
            resp = self._client.cancel_open_orders(symbol=symbol)
            return {"ok": True, "symbol": symbol, "raw": resp}
        except Exception as exc:
            _log.error("cancel_open_orders failed for %s: %s", symbol, exc)
            return {"ok": False, "symbol": symbol, "error": str(exc)}

    def market_close(self, symbol: str) -> dict[str, Any]:
        """Flatten the position with a reduceOnly MARKET order in the closing
        direction. This is the AUTO-FLATTEN used when a stop can't be verified —
        it must NEVER open or increase a position, hence reduceOnly=true.

        Returns {ok, ...flat...} when already flat (nothing to do), the fill
        otherwise, or {ok:False, error}.
        """
        pos = self.get_position(symbol)
        qty = abs(_D(pos.get("qty")) or Decimal("0"))
        if not pos or qty <= 0 or pos.get("side") == "flat":
            return {"ok": True, "symbol": symbol, "flat": True,
                    "reason": "already flat — nothing to close"}
        # closing side is opposite the open side
        close_side = "SELL" if pos.get("side") == "long" else "BUY"
        try:
            resp = self._client.new_order(
                symbol=symbol, side=close_side, type="MARKET",
                quantity=str(qty), reduceOnly="true",
            )
        except Exception as exc:
            _log.error("market_close failed for %s: %s", symbol, exc)
            return {"ok": False, "symbol": symbol, "error": str(exc)}
        fill = _parse_fill(resp, symbol=symbol, side=close_side)
        fill["reduce_only"] = True
        fill["closed_side"] = pos.get("side")
        return fill

    def list_open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        """List open ALGO orders for `symbol` (so reconcile-on-start can verify a
        live stop exists). NEVER raises — empty list on failure."""
        try:
            rows = self._client.get_open_algo_orders(symbol=symbol) or []
            out = []
            for r in rows:
                out.append({
                    "order_id": r.get("orderId") or r.get("algoId"),
                    "symbol": r.get("symbol"),
                    "side": r.get("side"),
                    "type": r.get("type"),
                    "stop_price": float(_D(r.get("stopPrice")) or 0),
                    "close_position": str(r.get("closePosition")).lower() == "true",
                    "raw": r,
                })
            return out
        except Exception as exc:
            _log.warning("list_open_algo_orders failed for %s: %s", symbol, exc)
            return []


# ---------------------------------------------------------------------------
# small free helpers
# ---------------------------------------------------------------------------

def _norm_side(side: Any) -> Optional[str]:
    """Map long/short/buy/sell -> BUY/SELL. None if unrecognized."""
    if side is None:
        return None
    s = str(side).strip().upper()
    if s in ("BUY", "LONG"):
        return "BUY"
    if s in ("SELL", "SHORT"):
        return "SELL"
    return None


def _is_no_need_error(exc: Exception) -> bool:
    """True if the exception is Binance's -4046 'no need to change' (idempotent
    margin/leverage call) — which we treat as success. Tolerant of the connector's
    ClientError shape (.error_code) and of plain messages containing -4046."""
    code = getattr(exc, "error_code", None)
    try:
        if code is not None and int(code) == _BINANCE_NO_NEED_CODE:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(exc)
    return "-4046" in msg or "no need to change" in msg.lower()


def _parse_fill(resp: Any, *, symbol: str, side: str) -> dict[str, Any]:
    """Normalize a new_order response into a fill dict. Computes avg fill price
    and total fee from the fills[] array (real fees), falling back to avgPrice."""
    if not isinstance(resp, dict):
        return {"ok": True, "symbol": symbol, "side": side, "raw": resp,
                "avg_price": None, "fee": None, "order_id": None, "qty": None}
    fills = resp.get("fills") or []
    total_qty = Decimal("0")
    total_quote = Decimal("0")
    total_fee = Decimal("0")
    for f in fills:
        fq = _D(f.get("qty")) or Decimal("0")
        fp = _D(f.get("price")) or Decimal("0")
        fc = _D(f.get("commission")) or Decimal("0")
        total_qty += fq
        total_quote += fq * fp
        total_fee += fc
    if total_qty > 0:
        avg_price = float(total_quote / total_qty)
    else:
        ap = _D(resp.get("avgPrice"))
        avg_price = float(ap) if ap is not None and ap > 0 else None
    executed = _D(resp.get("executedQty"))
    qty_out = float(executed) if executed is not None else (
        float(total_qty) if total_qty > 0 else None)
    return {
        "ok": True,
        "order_id": resp.get("orderId"),
        "avg_price": avg_price,
        "fee": float(total_fee) if fills else None,
        "qty": qty_out,
        "side": side,
        "symbol": symbol,
        "status": resp.get("status"),
        "raw": resp,
    }
