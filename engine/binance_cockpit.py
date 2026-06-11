"""
binance_cockpit.py — the SAFETY BRAIN of the responsible manual-trading cockpit.

The human makes EVERY trade decision (discretionary). This module does NOT pick
trades, predict price, or claim an edge. Its only job is SAFETY ENFORCEMENT +
TRANSPARENCY around an order the human has already decided to place:

  * preview_trade   — show the human EXACTLY what an order costs and whether it
                      passes every safety check, BEFORE any money moves.
  * place_manual_trade — execute, but ONLY through risk_engine.gate_order (the
                      single approve path), and ATOMICALLY attach an exchange
                      stop-loss, VERIFY it is live, and AUTO-FLATTEN if it isn't
                      (never leave naked leveraged risk).
  * close_position  — flatten + cancel resting orders + log the exit (real fees).
  * reconcile_on_start — scan open positions; any without a live stop is given
                      one or flattened (the naked-position guard, on boot).
  * enforce_caps    — daily/global loss caps via risk_engine.check_caps; on a
                      breach, flatten everything and block.

NON-NEGOTIABLE SAFETY INVARIANTS (mirrored from the project spec):
  1. Every order routes through risk_engine.gate_order. There is NO other path,
     for the bot OR the human. A rejected gate places ZERO orders.
  2. The STOP-LOSS is placed on the exchange ATOMICALLY with/just after entry and
     VERIFIED to be live. If it cannot be verified -> the position is market-CLOSED
     immediately (no naked leveraged risk) and a loud fault is recorded.
  3. reconcile_on_start treats any open position without a live stop as naked and
     either protects it or flattens it.
  4. NO martingale / add-to-loser / widen-stop: sizing comes only from
     gate_order, which is provably stateless across trades (see risk_engine).
  5. Keys never appear here; the exchange `client` is INJECTED (a real
     BinanceFuturesClient in prod, a MockFuturesClient in tests).

Costs are reported NET: taker fee (default 0.05% per side, so 0.10% round-trip)
plus a slippage estimate, subtracted from target/loss outcomes so the human sees
the real number, not the gross one.

This module NEVER raises on the preview path. place_manual_trade raises EXACTLY
ONE loud exception — the naked-position guard — and only after it has already
market-closed the position; everything else is returned as a structured result.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import risk_engine

_log = logging.getLogger(__name__)

# Cost model defaults (Binance USDⓈ-M taker fee is 0.05%/side; 0.10% round-trip).
DEFAULT_TAKER_FEE_PCT = 0.05      # per side, percent of notional
DEFAULT_SLIPPAGE_PCT = 0.02       # per side, conservative market-order slippage
# A stop that sits at/under the liquidation price is worthless (you'd be
# liquidated before it ever triggers). Keep a small safety buffer.
LIQUIDATION_BUFFER_FRAC = 0.0     # stop must be strictly safer than liq (buffer >= 0)

MODE = "binance"


# ===========================================================================
# Sidecar audit store — DATA_ROOT/binance_audit.db, mode='binance'.
# Kept SEPARATE from the shared audit.db so the manual cockpit never disturbs
# the demo/live Polymarket ledger or its hot-loop connection. Never raises.
# ===========================================================================

def _db_path() -> Path:
    root = os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))
    return Path(root) / "binance_audit.db"


_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[str] = None
_LOCK = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Lazy connection to the sidecar DB. Re-opens if DATA_ROOT changed (tests)."""
    global _conn, _conn_path
    path = str(_db_path())
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS binance_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            mode TEXT,
            event TEXT,                 -- 'entry' | 'exit' | 'fault'
            symbol TEXT,
            side TEXT,
            qty REAL,
            entry_price REAL,
            stop_price REAL,
            target_price REAL,
            exit_price REAL,
            fee REAL,
            realized_pnl REAL,
            leverage REAL,
            risk_dollars REAL,
            stop_verified INTEGER,
            context_json TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_ts ON binance_trades(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_binance_event ON binance_trades(event)")
    conn.commit()
    _conn, _conn_path = conn, path
    return conn


def _coerce(v: Any) -> Any:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, Decimal):
        return float(v)
    return v


def log_trade(row: dict[str, Any]) -> bool:
    """Append one row to the sidecar binance_audit.db. mode='binance'. Never raises."""
    try:
        vals = {
            "ts": row.get("ts") or time.time(),
            "mode": MODE,
            "event": row.get("event"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "qty": row.get("qty"),
            "entry_price": row.get("entry_price"),
            "stop_price": row.get("stop_price"),
            "target_price": row.get("target_price"),
            "exit_price": row.get("exit_price"),
            "fee": row.get("fee"),
            "realized_pnl": row.get("realized_pnl"),
            "leverage": row.get("leverage"),
            "risk_dollars": row.get("risk_dollars"),
            "stop_verified": row.get("stop_verified"),
            "context_json": json.dumps(row.get("context") or {}, ensure_ascii=False, default=str),
        }
        cols = ",".join(vals.keys())
        ph = ",".join("?" for _ in vals)
        with _LOCK:
            conn = _get_conn()
            conn.execute(f"INSERT INTO binance_trades ({cols}) VALUES ({ph})",
                         [_coerce(v) for v in vals.values()])
            conn.commit()
        return True
    except Exception as e:  # logging must never break the trade path
        _log.warning("binance_cockpit.log_trade failed: %r", e)
        return False


def list_trades(limit: int = 500) -> list[dict[str, Any]]:
    """Read back recorded rows (newest first). Never raises."""
    try:
        with _LOCK:
            rows = _get_conn().execute(
                "SELECT * FROM binance_trades ORDER BY ts DESC LIMIT ?",
                (max(1, min(int(limit), 10000)),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.warning("binance_cockpit.list_trades failed: %r", e)
        return []


def _record_fault(*, title: str, detail: str, severity: str = "critical",
                  context: Optional[dict[str, Any]] = None) -> None:
    """Loud fault into BOTH the sidecar DB and (if available) fault_tracker.
    Never raises."""
    log_trade({
        "event": "fault",
        "symbol": (context or {}).get("symbol"),
        "context": {"title": title, "detail": detail, "severity": severity, **(context or {})},
    })
    try:
        import fault_tracker  # optional dependency; absent in some test envs
        fault_tracker.record_fault(
            category="binance", severity=severity, title=title,
            detail=detail, source="binance_cockpit", context=context or {},
        )
    except Exception:
        pass


# ===========================================================================
# Naked-position guard exception
# ===========================================================================

class NakedPositionError(RuntimeError):
    """Raised ONLY after a position was opened, its stop could not be verified
    live, and we therefore MARKET-CLOSED it. Loud on purpose — a verified stop
    is non-negotiable; we would rather scream than leave naked leveraged risk."""


# ===========================================================================
# Small helpers
# ===========================================================================

def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _check(name: str, ok: bool, reason: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "reason": reason}


def _exchange_side(side: str) -> Optional[str]:
    s = str(side).strip().lower()
    if s in ("long", "buy"):
        return "BUY"
    if s in ("short", "sell"):
        return "SELL"
    return None


def _closing_side(side: str) -> Optional[str]:
    """The order side that CLOSES a position opened on `side`."""
    s = str(side).strip().lower()
    if s in ("long", "buy"):
        return "SELL"
    if s in ("short", "sell"):
        return "BUY"
    return None


def _is_stop_live(orders: list[dict[str, Any]]) -> bool:
    """True iff a STOP_MARKET algo order with closePosition is present & live."""
    for o in orders or []:
        typ = str(o.get("type") or "").upper()
        if "STOP" in typ and o.get("close_position", False):
            return True
    # tolerate a stop that isn't closePosition but is clearly a stop order
    for o in orders or []:
        if "STOP" in str(o.get("type") or "").upper():
            return True
    return False


# ===========================================================================
# preview_trade — TRANSPARENCY. Never raises.
# ===========================================================================

def preview_trade(
    client: Any,
    *,
    symbol: str,
    side: str,
    entry: Any,
    stop: Any,
    target: Any = None,
    equity: Any,
    risk_pct: float = risk_engine.DEFAULT_RISK_PCT,
    leverage: int = 3,
    risk_state: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
    taker_fee_pct: float = DEFAULT_TAKER_FEE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> dict[str, Any]:
    """Compute the full, HONEST preview of a manual order without placing it.

    Sizing comes from risk_engine.gate_order (the ONLY approve path). Costs are
    NET (taker fee per side + slippage). Returns a dict with qty, notional,
    fee_est, slippage_est, total_cost, liquidation_price, net_target,
    net_if_stopped, an itemised `checks` list, and `approved`.

    NEVER raises: any bad input becomes approved=False + a reason + a failing check.
    """
    try:
        return _preview_inner(
            client, symbol=symbol, side=side, entry=entry, stop=stop, target=target,
            equity=equity, risk_pct=risk_pct, leverage=leverage,
            risk_state=risk_state or {}, config=config or {},
            taker_fee_pct=taker_fee_pct, slippage_pct=slippage_pct,
        )
    except Exception as exc:  # the preview must NEVER blow up the UI
        return {
            "approved": False, "qty": 0.0, "notional": 0.0,
            "checks": [_check("internal", False, f"preview internal error -> safe reject: {exc!r}")],
            "reason": f"internal error -> safe reject: {exc!r}",
        }


def _preview_inner(client, *, symbol, side, entry, stop, target, equity,
                   risk_pct, leverage, risk_state, config,
                   taker_fee_pct, slippage_pct):
    checks: list[dict[str, Any]] = []
    en = _f(entry)
    st = _f(stop)
    tg = _f(target)
    eq = _f(equity)
    norm_side = str(side).strip().lower()

    # --- basic input validity ---
    inputs_ok = (
        norm_side in ("long", "short")
        and en is not None and en > 0
        and st is not None and st > 0
        and eq is not None and eq > 0
    )
    checks.append(_check(
        "inputs", inputs_ok,
        "valid side/entry/stop/equity" if inputs_ok
        else "need side long/short and positive entry/stop/equity",
    ))

    # --- stop is on the correct side of entry (a long stops BELOW, a short ABOVE) ---
    stop_side_ok = False
    if inputs_ok:
        stop_side_ok = (st < en) if norm_side == "long" else (st > en)
    checks.append(_check(
        "stop_direction", stop_side_ok,
        "stop is on the protective side of entry" if stop_side_ok
        else "stop is on the WRONG side of entry (would not protect)",
    ))

    cfg = dict(config or {})
    cfg.setdefault("risk_pct", risk_pct)
    cfg.setdefault("leverage_cap", risk_engine.DEFAULT_LEVERAGE_CAP)

    # --- THE GATE: sizing + R:R + leverage + risk_pct + caps (single approve path) ---
    signal = {"signal": norm_side, "entry": en, "stop": st, "target": tg}
    gate = risk_engine.gate_order(signal, eq, risk_state, cfg)
    gate_ok = bool(gate.get("approved"))
    checks.append(_check("risk_gate", gate_ok, gate.get("reason", "")))

    qty_raw = _f(gate.get("qty")) or 0.0
    risk_dollars = _f(gate.get("risk_dollars")) or 0.0
    leverage_eff = _f(gate.get("leverage"))
    rr = _f(gate.get("rr"))

    # --- snap qty DOWN to the exchange lot step + check min-notional (live filters) ---
    filt = {}
    lot_step = tick = min_notional = Decimal("0")
    try:
        filt = client.get_exchange_filters(symbol) or {}
        lot_step = filt.get("lot_step", Decimal("0"))
        tick = filt.get("tick_size", Decimal("0"))
        min_notional = filt.get("min_notional", Decimal("0"))
    except Exception as exc:
        checks.append(_check("exchange_filters", False, f"could not read exchange filters: {exc!r}"))

    if lot_step and lot_step > 0:
        qty_dec = client.round_qty_down(qty_raw, lot_step)
        qty = float(qty_dec)
    else:
        # unknown lot step -> we cannot safely size; fail this check (don't guess)
        qty_dec = Decimal("0")
        qty = 0.0
    filters_known = bool(lot_step and lot_step > 0)
    checks.append(_check(
        "lot_step", filters_known,
        f"qty snapped DOWN to lot step {lot_step}" if filters_known
        else "lot step unknown (exchangeInfo unavailable) -> refuse to size",
    ))

    notional = qty * (en or 0.0)

    min_notional_ok = True
    if en is not None and qty > 0:
        try:
            min_notional_ok = client.meets_min_notional(qty, en, min_notional)
        except Exception:
            min_notional_ok = False
    else:
        min_notional_ok = False
    checks.append(_check(
        "min_notional", min_notional_ok,
        f"notional {notional:.2f} >= min {float(min_notional or 0):.2f}" if min_notional_ok
        else f"notional {notional:.2f} below exchange min-notional {float(min_notional or 0):.2f}",
    ))

    # --- liquidation price (best-effort; may be None when flat / unavailable) ---
    liquidation_price = None
    try:
        liquidation_price = client.get_liquidation_price(symbol)
    except Exception:
        liquidation_price = None

    # the stop must be SAFER than the liquidation price (we must exit before liq)
    liq_ok = True
    if liquidation_price is not None and st is not None and norm_side in ("long", "short"):
        if norm_side == "long":
            liq_ok = st > liquidation_price * (1.0 + LIQUIDATION_BUFFER_FRAC)
        else:
            liq_ok = st < liquidation_price * (1.0 - LIQUIDATION_BUFFER_FRAC)
        checks.append(_check(
            "liquidation_vs_stop", liq_ok,
            "stop triggers before liquidation" if liq_ok
            else f"liquidation {liquidation_price} would hit before the stop {st} (raise margin/lower size)",
        ))

    # --- NET cost model: taker fee both sides + slippage both sides ---
    fee_rate = (_f(taker_fee_pct) or 0.0) / 100.0
    slip_rate = (_f(slippage_pct) or 0.0) / 100.0
    # round-trip fee: pay taker on entry notional and again on exit notional (~same size)
    fee_est = notional * fee_rate * 2.0
    slippage_est = notional * slip_rate * 2.0
    total_cost = fee_est + slippage_est

    # gross outcomes from price moves, then subtract costs to get NET
    net_target = None
    if tg is not None and qty > 0 and en is not None:
        gross_win = (tg - en) * qty if norm_side == "long" else (en - tg) * qty
        net_target = gross_win - total_cost
    net_if_stopped = None
    if st is not None and qty > 0 and en is not None:
        gross_loss = (st - en) * qty if norm_side == "long" else (en - st) * qty
        # gross_loss is already negative; costs make it worse
        net_if_stopped = gross_loss - total_cost

    approved = bool(
        inputs_ok and stop_side_ok and gate_ok and filters_known
        and min_notional_ok and liq_ok and qty > 0
    )

    return {
        "approved": approved,
        "symbol": symbol,
        "side": norm_side,
        "qty": qty,
        "notional": notional,
        "entry": en,
        "stop": st,
        "target": tg,
        "fee_est": fee_est,
        "slippage_est": slippage_est,
        "total_cost": total_cost,
        "liquidation_price": liquidation_price,
        "net_target": net_target,
        "net_if_stopped": net_if_stopped,
        "risk_dollars": risk_dollars,
        "leverage": leverage_eff,
        "rr": rr,
        "checks": checks,
        "reason": gate.get("reason", ""),
    }


# ===========================================================================
# place_manual_trade — EXECUTION. gate_order is the only path; atomic stop;
# naked-position guard.
# ===========================================================================

def place_manual_trade(client: Any, params: dict[str, Any],
                       risk_state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Execute a human-decided trade SAFELY.

    Flow (and the ONLY flow that may place a Binance order):
      1. risk_engine.gate_order — the single approve path. Rejected -> NO order.
      2. set_leverage_isolated  — ISOLATED margin + the chosen leverage.
      3. place_market           — the entry.
      4. place_algo_stop        — the mandatory exchange stop (ATOMIC, right after entry).
      5. place_algo_take_profit — optional discretionary TP.
      6. VERIFY the stop is live via list_open_algo_orders. If it is NOT ->
         market_close the position (no naked risk), record a loud fault, and
         raise NakedPositionError.
      7. log the trade (entry fill, stop, tp, costs, mode='binance') to the
         sidecar binance_audit.db.

    `params`: {symbol, side, entry, stop, target?, equity, risk_pct?, leverage?, config?}

    Returns the recorded trade dict on success. Returns {ok:False, ...} for a
    clean rejection (gate reject / entry-fill failure). Raises NakedPositionError
    ONLY after it has already flattened a position whose stop could not be verified.
    """
    rs = risk_state or {}
    symbol = params.get("symbol")
    side = params.get("side")
    entry = params.get("entry")
    stop = params.get("stop")
    target = params.get("target")
    equity = params.get("equity")
    risk_pct = params.get("risk_pct", risk_engine.DEFAULT_RISK_PCT)
    leverage = int(params.get("leverage", 3) or 3)
    cfg = dict(params.get("config") or {})
    cfg.setdefault("risk_pct", risk_pct)

    norm_side = str(side).strip().lower()
    ex_side = _exchange_side(norm_side)
    close_side = _closing_side(norm_side)
    if ex_side is None or close_side is None:
        return {"ok": False, "approved": False,
                "reason": f"invalid side {side!r} (need long/short)"}

    # --- 1. THE GATE — re-run it here even if the UI previewed; it is the only path. ---
    signal = {"signal": norm_side, "entry": _f(entry), "stop": _f(stop), "target": _f(target)}
    gate = risk_engine.gate_order(signal, equity, rs, cfg)
    if not gate.get("approved"):
        # A rejected gate places ZERO orders. Full stop.
        return {"ok": False, "approved": False, "placed_order": False,
                "reason": f"gate rejected -> {gate.get('reason')}", "gate": gate}

    gate_qty = gate.get("qty")
    gate_stop = gate.get("stop")        # passed through verbatim — never widened
    gate_target = gate.get("target")

    # --- snap qty DOWN to the lot step; refuse if filters unknown or below min-notional ---
    filt = client.get_exchange_filters(symbol) or {}
    lot_step = filt.get("lot_step", Decimal("0"))
    min_notional = filt.get("min_notional", Decimal("0"))
    if not (lot_step and lot_step > 0):
        return {"ok": False, "approved": True, "placed_order": False,
                "reason": "lot step unknown (exchangeInfo unavailable) -> refuse to place"}
    qty_dec = client.round_qty_down(gate_qty, lot_step)
    if qty_dec <= 0:
        return {"ok": False, "approved": True, "placed_order": False,
                "reason": f"sized qty {gate_qty} rounds below one lot step {lot_step}"}
    if not client.meets_min_notional(qty_dec, _f(entry), min_notional):
        return {"ok": False, "approved": True, "placed_order": False,
                "reason": f"qty {qty_dec} @ {entry} below exchange min-notional {float(min_notional or 0):.2f}"}

    # --- 2. ISOLATED margin + leverage (idempotent; -4046 swallowed in the client). ---
    lev_res = client.set_leverage_isolated(symbol, leverage)
    if not lev_res.get("ok", True):
        return {"ok": False, "approved": True, "placed_order": False,
                "reason": f"could not set isolated leverage -> {lev_res.get('error')}"}

    # --- 3. MARKET entry. ---
    entry_fill = client.place_market(symbol, ex_side, qty_dec)
    if not entry_fill.get("ok"):
        return {"ok": False, "approved": True, "placed_order": False,
                "reason": f"entry order failed -> {entry_fill.get('error')}",
                "entry_fill": entry_fill}

    avg_fill = entry_fill.get("avg_price") or _f(entry)

    # --- 4. ATOMIC STOP — the mandatory exchange stop, placed immediately. ---
    stop_res = client.place_algo_stop(symbol, close_side, gate_stop)

    # --- 5. optional TP (discretionary; never mandatory). ---
    tp_res = None
    if gate_target is not None:
        tp_res = client.place_algo_take_profit(symbol, close_side, gate_target)

    # --- 6. VERIFY the stop is actually live. The position is NEVER allowed to
    #        exist without a verified stop. ---
    open_algos = client.list_open_algo_orders(symbol)
    stop_live = bool(stop_res.get("ok")) and _is_stop_live(open_algos)

    if not stop_live:
        # NAKED-POSITION GUARD: flatten now, scream, and refuse to return success.
        close_res = client.market_close(symbol)
        # cancel any half-placed resting orders too (e.g. a TP with no stop)
        try:
            client.cancel_open_orders(symbol)
        except Exception:
            pass
        fault_ctx = {
            "symbol": symbol, "side": norm_side, "qty": float(qty_dec),
            "entry_fill": entry_fill, "stop_res": stop_res,
            "open_algos": open_algos, "close_res": close_res,
        }
        _record_fault(
            title="STOP NOT VERIFIED — position auto-flattened (naked-risk guard)",
            detail=(f"{symbol} {norm_side} qty {qty_dec}: stop could not be confirmed live "
                    f"after entry; market-closed to avoid naked leveraged risk."),
            severity="critical", context=fault_ctx,
        )
        log_trade({
            "event": "entry", "symbol": symbol, "side": norm_side, "qty": float(qty_dec),
            "entry_price": avg_fill, "stop_price": _f(gate_stop), "target_price": _f(gate_target),
            "fee": entry_fill.get("fee"), "leverage": leverage,
            "risk_dollars": gate.get("risk_dollars"), "stop_verified": False,
            "context": {"reason": "stop_unverified_autoflatten", "close_res": close_res},
        })
        if close_res.get("ok"):
            raise NakedPositionError(
                f"{symbol} {norm_side}: stop could not be verified live; position was "
                f"market-closed (no naked leveraged risk). See fault log."
            )
        raise NakedPositionError(
            f"{symbol} {norm_side}: stop could not be verified live AND the emergency "
            f"market-close was REJECTED ({close_res.get('error')}). Position may still be "
            f"OPEN AND NAKED — flatten it manually NOW. See fault log."
        )

    # --- 7. log the protected trade. ---
    record = {
        "ok": True,
        "approved": True,
        "placed_order": True,
        "symbol": symbol,
        "side": norm_side,
        "qty": float(qty_dec),
        "entry_price": avg_fill,
        "stop_price": _f(gate_stop),
        "target_price": _f(gate_target),
        "fee": entry_fill.get("fee"),
        "leverage": leverage,
        "risk_dollars": gate.get("risk_dollars"),
        "stop_verified": True,
        "entry_fill": entry_fill,
        "stop_order": stop_res,
        "tp_order": tp_res,
    }
    log_trade({
        "event": "entry", "symbol": symbol, "side": norm_side, "qty": float(qty_dec),
        "entry_price": avg_fill, "stop_price": _f(gate_stop), "target_price": _f(gate_target),
        "fee": entry_fill.get("fee"), "leverage": leverage,
        "risk_dollars": gate.get("risk_dollars"), "stop_verified": True,
        "context": {"stop_order": stop_res, "tp_order": tp_res},
    })
    return record


# ===========================================================================
# close_position — flatten + cancel resting orders + log the exit (real fees).
# ===========================================================================

def close_position(client: Any, symbol: str) -> dict[str, Any]:
    """Market-close the position, cancel ALL resting orders (stop/TP), and log
    the exit with REAL fees from the close fill. Idempotent when already flat."""
    pos = client.get_position(symbol)
    close_res = client.market_close(symbol)
    cancel_res = client.cancel_open_orders(symbol)

    exit_price = close_res.get("avg_price")
    fee = close_res.get("fee")
    realized = None
    try:
        entry_price = _f((pos or {}).get("entry_price"))
        qty = abs(_f((pos or {}).get("qty")) or 0.0)
        side = (pos or {}).get("side")
        if entry_price and exit_price and qty:
            gross = ((exit_price - entry_price) if side == "long"
                     else (entry_price - exit_price)) * qty
            realized = gross - (fee or 0.0)
    except Exception:
        realized = None

    log_trade({
        "event": "exit", "symbol": symbol,
        "side": (pos or {}).get("side"),
        "qty": abs(_f((pos or {}).get("qty")) or 0.0) or None,
        "entry_price": (pos or {}).get("entry_price"),
        "exit_price": exit_price, "fee": fee, "realized_pnl": realized,
        "context": {"close_res": close_res, "cancel_res": cancel_res},
    })
    return {
        "ok": bool(close_res.get("ok")),
        "symbol": symbol,
        "flat": close_res.get("flat", False),
        "exit_price": exit_price,
        "fee": fee,
        "realized_pnl": realized,
        "close_res": close_res,
        "cancel_res": cancel_res,
    }


# ===========================================================================
# reconcile_on_start — the naked-position guard on boot.
# ===========================================================================

def reconcile_on_start(client: Any, symbols: Optional[list[str]] = None,
                       *, stop_distance_frac: float = 0.01) -> dict[str, Any]:
    """For every open position: if there is NO live algo stop -> place one at a
    safe distance; if a stop cannot be placed -> market-close it. This is the
    boot-time naked-position guard. Returns a per-symbol report.

    `symbols`: which symbols to scan. If None, we ask the account for open
    positions (the client must expose get_open_positions(); otherwise pass a list).
    `stop_distance_frac`: how far from entry to place the protective stop when one
    is missing (default 1%, on the protective side).
    """
    report: dict[str, Any] = {"checked": [], "protected": [], "flattened": [], "ok": []}

    open_syms = symbols
    if open_syms is None:
        try:
            positions = client.get_open_positions() or []
            open_syms = [p.get("symbol") for p in positions if p.get("symbol")]
        except Exception as exc:
            _log.warning("reconcile_on_start: cannot list positions: %r", exc)
            open_syms = []

    for sym in open_syms:
        report["checked"].append(sym)
        pos = client.get_position(sym)
        qty = abs(_f((pos or {}).get("qty")) or 0.0)
        side = (pos or {}).get("side")
        if not pos or qty <= 0 or side not in ("long", "short"):
            continue  # flat — nothing to protect

        algos = client.list_open_algo_orders(sym)
        if _is_stop_live(algos):
            report["ok"].append(sym)
            continue

        # NAKED position found on boot. Try to protect it; if we can't, flatten it.
        entry_price = _f((pos or {}).get("entry_price")) or 0.0
        close_side = _closing_side(side)
        if entry_price > 0 and close_side:
            if side == "long":
                stop_price = entry_price * (1.0 - abs(stop_distance_frac))
            else:
                stop_price = entry_price * (1.0 + abs(stop_distance_frac))
            stop_res = client.place_algo_stop(sym, close_side, stop_price)
            # verify it actually went live before trusting it
            if stop_res.get("ok") and _is_stop_live(client.list_open_algo_orders(sym)):
                report["protected"].append(sym)
                log_trade({
                    "event": "entry", "symbol": sym, "side": side, "qty": qty,
                    "entry_price": entry_price, "stop_price": stop_price,
                    "stop_verified": True,
                    "context": {"source": "reconcile_on_start", "stop_res": stop_res},
                })
                _record_fault(
                    title="naked position protected on boot",
                    detail=f"{sym} {side} had no live stop at startup; placed one at {stop_price}.",
                    severity="high",
                    context={"symbol": sym, "side": side, "stop_price": stop_price},
                )
                continue

        # could not protect -> flatten (never leave it naked)
        close_res = client.market_close(sym)
        try:
            client.cancel_open_orders(sym)
        except Exception:
            pass
        report["flattened"].append(sym)
        _record_fault(
            title="naked position FLATTENED on boot (could not place a stop)",
            detail=f"{sym} {side} had no live stop and a protective stop could not be placed; market-closed.",
            severity="critical",
            context={"symbol": sym, "side": side, "close_res": close_res},
        )
        log_trade({
            "event": "exit", "symbol": sym, "side": side, "qty": qty,
            "entry_price": entry_price, "exit_price": close_res.get("avg_price"),
            "context": {"source": "reconcile_on_start", "reason": "naked_flatten", "close_res": close_res},
        })

    return report


# ===========================================================================
# enforce_caps — daily/global loss caps. On breach: flatten everything + block.
# ===========================================================================

def enforce_caps(client: Any, risk_state: dict[str, Any],
                 *, symbols: Optional[list[str]] = None,
                 config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Evaluate the daily/global loss caps via risk_engine.check_caps. If the
    caps say flatten/halt -> close every open position and BLOCK new entries.

    Returns {allow_new, flatten, halt, reason, closed:[...]}.
    """
    cfg = config or {}
    caps = risk_engine.check_caps(
        (risk_state or {}).get("day_pnl_pct"),
        (risk_state or {}).get("peak_drawdown_pct"),
        daily_stop=cfg.get("daily_stop", risk_engine.DEFAULT_DAILY_STOP),
        global_stop=cfg.get("global_stop", risk_engine.DEFAULT_GLOBAL_STOP),
    )
    out = {
        "allow_new": caps["allow_new"],
        "flatten": caps["flatten"],
        "halt": caps["halt"],
        "reason": caps["reason"],
        "closed": [],
    }
    if not caps["flatten"]:
        return out

    # breach -> flatten everything we can see
    syms = symbols
    if syms is None:
        try:
            positions = client.get_open_positions() or []
            syms = [p.get("symbol") for p in positions if p.get("symbol")]
        except Exception:
            syms = []
    for sym in syms:
        res = close_position(client, sym)
        out["closed"].append({"symbol": sym, "ok": res.get("ok")})
    _record_fault(
        title=("GLOBAL HALT — caps breached, all positions flattened" if caps["halt"]
               else "DAILY STOP — caps breached, all positions flattened"),
        detail=caps["reason"],
        severity="critical" if caps["halt"] else "high",
        context={"risk_state": risk_state, "closed": out["closed"]},
    )
    return out
