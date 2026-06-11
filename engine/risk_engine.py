"""
risk_engine.py — the MANDATORY risk layer between any signal and any order.

This is the second harmless-half module of the BTC TA system. There is **zero
real-money code here** — no exchange, no live orders, no network. It only does
arithmetic: how big may a position be, and may a new position be opened at all.

It is the ONLY approve path to an order. The bot loop AND a human pressing a
"buy" button must both go through `gate_order`. Nothing else may size or open.

Design rules (from the spec / safety floor):
  * Fixed-fractional sizing: qty chosen so |entry - stop| * qty == equity *
    risk_pct/100. The STOP sets the size, never the reverse.
  * Reject (size 0 + reason) when:
      - R:R < min_rr (default 2:1), when a target is provided
      - the stop distance implies effective leverage > leverage_cap
      - risk_pct > max_risk_pct (default 2%, the hard ceiling)
      - equity / prices are non-positive / non-finite / malformed
  * Loss caps: daily -3% -> flatten + block new entries; global -10% -> halt
    (block + flatten, manual re-enable elsewhere). Unknown PnL fails SAFE (block).
  * HARD invariants, enforced by construction (and asserted in tests):
      - NEVER increases size after a loss (sizing depends only on the CURRENT
        equity + risk_pct + stop distance; there is no prior-outcome input that
        can scale it up).
      - NEVER widens a stop (it sizes against the stop it is GIVEN; it returns
        that same stop and never moves it away from entry).
      - NO martingale multiplier (there is no doubling / averaging / recovery
        code path; the engine is stateless across trades).
  * NEVER raises on the hot path: any bad input -> a well-formed safe reject.

All public functions return plain dicts (JSON-friendly), deterministic, pure.
"""
from __future__ import annotations

from typing import Any, Optional

# Defaults mirror the spec's safety floor.
DEFAULT_RISK_PCT = 0.5         # 0.5% risk per trade
DEFAULT_MAX_RISK_PCT = 2.0     # hard ceiling: never risk more than 2%
DEFAULT_LEVERAGE_CAP = 3.0     # <=3x effective leverage
DEFAULT_MIN_RR = 2.0           # >=2:1 reward:risk
DEFAULT_DAILY_STOP = -3.0      # daily -3% -> flatten + block
DEFAULT_GLOBAL_STOP = -10.0    # global -10% -> halt


# ---------------------------------------------------------------------------
# Safe numeric helper
# ---------------------------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Never raises."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / +-inf
        return None
    return v


def _reject(reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "approved": False,
        "qty": 0,
        "risk_dollars": 0.0,
        "risk_per_unit": None,
        "leverage": None,
        "reason": reason,
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Fixed-fractional position sizing
# ---------------------------------------------------------------------------

def size_position(
    equity: Any,
    entry: Any,
    stop: Any,
    *,
    risk_pct: float = DEFAULT_RISK_PCT,
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT,
    leverage_cap: float = DEFAULT_LEVERAGE_CAP,
    target: Any = None,
    min_rr: float = DEFAULT_MIN_RR,
) -> dict[str, Any]:
    """Fixed-fractional sizing. The STOP sets the size, never the reverse.

    qty is chosen so that  |entry - stop| * qty == equity * risk_pct / 100.

    Returns a dict:
        {approved, qty, risk_dollars, risk_per_unit, leverage, reason[, rr]}

    On any rejection: approved=False, qty=0, and a human-readable reason.
    Never raises.
    """
    try:
        return _size_position_inner(
            equity, entry, stop,
            risk_pct=risk_pct, max_risk_pct=max_risk_pct,
            leverage_cap=leverage_cap, target=target, min_rr=min_rr,
        )
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        return _reject(f"internal error -> safe reject: {exc!r}")


def _size_position_inner(
    equity, entry, stop, *,
    risk_pct, max_risk_pct, leverage_cap, target, min_rr,
) -> dict[str, Any]:
    eq = _f(equity)
    en = _f(entry)
    st = _f(stop)
    rp = _f(risk_pct)
    maxrp = _f(max_risk_pct)
    lev_cap = _f(leverage_cap)
    mrr = _f(min_rr)

    if eq is None or eq <= 0:
        return _reject("equity must be a positive finite number")
    if en is None or en <= 0:
        return _reject("entry must be a positive finite number")
    if st is None or st <= 0:
        return _reject("stop must be a positive finite number")
    if rp is None or rp <= 0:
        return _reject("risk_pct must be a positive finite number")
    if maxrp is None or maxrp <= 0:
        return _reject("max_risk_pct must be a positive finite number")
    if lev_cap is None or lev_cap <= 0:
        return _reject("leverage_cap must be a positive finite number")
    if mrr is None:
        mrr = DEFAULT_MIN_RR

    # Hard ceiling: never risk more than max_risk_pct.
    if rp > maxrp:
        return _reject(
            f"risk_pct {rp:.3f}% exceeds max_risk_pct {maxrp:.3f}% (hard ceiling)"
        )

    risk_per_unit = abs(en - st)
    if risk_per_unit <= 0:
        return _reject("stop distance is zero (entry == stop) -> cannot size")

    # R:R gate — only when a target is supplied (sizing alone needn't know it).
    tg = _f(target)
    rr: Optional[float] = None
    if tg is not None:
        reward = abs(tg - en)
        rr = reward / risk_per_unit if risk_per_unit > 0 else 0.0
        if rr < mrr:
            return _reject(
                f"reward:risk {rr:.2f} < min_rr {mrr:.2f} (need >= {mrr:.1f}:1)",
                rr=rr, risk_per_unit=risk_per_unit,
            )

    # Fixed-fractional: dollars at risk, then qty from the stop distance.
    risk_dollars = eq * rp / 100.0
    qty = risk_dollars / risk_per_unit

    # Effective leverage = notional / equity. A too-tight stop blows this up.
    notional = qty * en
    leverage = notional / eq
    if leverage > lev_cap:
        return _reject(
            f"effective leverage {leverage:.2f}x exceeds cap {lev_cap:.2f}x "
            f"(stop too tight for this risk_pct)",
            risk_per_unit=risk_per_unit, leverage=leverage, rr=rr,
        )

    out: dict[str, Any] = {
        "approved": True,
        "qty": qty,
        "risk_dollars": risk_dollars,
        "risk_per_unit": risk_per_unit,
        "leverage": leverage,
        "notional": notional,
        "reason": (
            f"fixed-fractional: risk {rp:.3f}% of {eq:.2f} = ${risk_dollars:.2f}; "
            f"stop distance {risk_per_unit:.4f} -> qty {qty:.6f}; "
            f"leverage {leverage:.2f}x <= {lev_cap:.1f}x"
        ),
    }
    if rr is not None:
        out["rr"] = rr
    return out


# ---------------------------------------------------------------------------
# Loss caps — daily + global
# ---------------------------------------------------------------------------

def check_caps(
    day_pnl_pct: Any,
    peak_drawdown_pct: Any,
    *,
    daily_stop: float = DEFAULT_DAILY_STOP,
    global_stop: float = DEFAULT_GLOBAL_STOP,
) -> dict[str, Any]:
    """Evaluate the daily + global loss caps.

    Args:
        day_pnl_pct: today's realised+unrealised PnL as a percent (negative = loss).
        peak_drawdown_pct: drawdown from the equity peak as a percent (negative).
        daily_stop: daily loss limit (default -3%): at/under -> flatten + block.
        global_stop: global loss limit (default -10%): at/under -> HALT.

    Returns:
        {allow_new, flatten, halt, reason}

    Fails SAFE: unknown/garbled PnL -> block new entries (never crashes).
    """
    try:
        return _check_caps_inner(
            day_pnl_pct, peak_drawdown_pct,
            daily_stop=daily_stop, global_stop=global_stop,
        )
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        return {
            "allow_new": False, "flatten": False, "halt": False,
            "reason": f"internal error -> fail safe (block new): {exc!r}",
        }


def _check_caps_inner(day_pnl_pct, peak_drawdown_pct, *, daily_stop, global_stop):
    day = _f(day_pnl_pct)
    peak = _f(peak_drawdown_pct)
    ds = _f(daily_stop)
    gs = _f(global_stop)
    if ds is None:
        ds = DEFAULT_DAILY_STOP
    if gs is None:
        gs = DEFAULT_GLOBAL_STOP

    # Unknown PnL must fail safe — block new entries (don't crash, don't open).
    if day is None or peak is None:
        return {
            "allow_new": False, "flatten": False, "halt": False,
            "reason": "unknown PnL (could not parse) -> fail safe: block new entries",
        }

    # Global halt takes precedence (the harsher state).
    if peak <= gs or day <= gs:
        worst = min(peak, day)
        return {
            "allow_new": False, "flatten": True, "halt": True,
            "reason": (
                f"global stop hit: drawdown {worst:.2f}% <= {gs:.1f}% -> HALT "
                f"(flatten + block; manual re-enable required)"
            ),
        }

    if day <= ds:
        return {
            "allow_new": False, "flatten": True, "halt": False,
            "reason": (
                f"daily stop hit: day PnL {day:.2f}% <= {ds:.1f}% -> "
                f"flatten + block new entries until next session"
            ),
        }

    return {
        "allow_new": True, "flatten": False, "halt": False,
        "reason": (
            f"within caps: day {day:.2f}% > {ds:.1f}%, "
            f"drawdown {peak:.2f}% > {gs:.1f}%"
        ),
    }


# ---------------------------------------------------------------------------
# gate_order — the ONLY approve path (bot AND manual must call this)
# ---------------------------------------------------------------------------

def gate_order(
    signal: Any,
    equity: Any,
    risk_state: Any,
    config: Any,
) -> dict[str, Any]:
    """The single gate every order must pass — bot loop AND manual button.

    Args:
        signal: a dict from btc_strategy.evaluate_signal (or a manual order in
            the same shape): {signal, entry, stop, target[, rr]}.
        equity: current account equity (the CURRENT value — sizing never looks
            at prior trade outcomes, so a loss can only SHRINK the next size).
        risk_state: {day_pnl_pct, peak_drawdown_pct, ...}. Any extra keys a
            caller might pass (loss_streak, last_loss, ...) are IGNORED — there
            is no input that can make this engine size up.
        config: optional overrides {risk_pct, max_risk_pct, leverage_cap,
            min_rr, daily_stop, global_stop}.

    Returns: {approved, qty, reason[, entry, stop, target, risk_dollars, ...]}.

    Never raises. On any problem -> approved=False, qty=0, with a reason.
    """
    try:
        return _gate_order_inner(signal, equity, risk_state, config)
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        return {
            "approved": False, "qty": 0,
            "reason": f"internal error -> safe reject: {exc!r}",
        }


def _gate_order_inner(signal, equity, risk_state, config):
    cfg = config if isinstance(config, dict) else {}
    rs = risk_state if isinstance(risk_state, dict) else {}

    risk_pct = cfg.get("risk_pct", DEFAULT_RISK_PCT)
    max_risk_pct = cfg.get("max_risk_pct", DEFAULT_MAX_RISK_PCT)
    leverage_cap = cfg.get("leverage_cap", DEFAULT_LEVERAGE_CAP)
    min_rr = cfg.get("min_rr", DEFAULT_MIN_RR)
    daily_stop = cfg.get("daily_stop", DEFAULT_DAILY_STOP)
    global_stop = cfg.get("global_stop", DEFAULT_GLOBAL_STOP)

    # --- 1. Loss caps first: if we're halted / past the daily stop, never size. ---
    caps = check_caps(
        rs.get("day_pnl_pct"), rs.get("peak_drawdown_pct"),
        daily_stop=daily_stop, global_stop=global_stop,
    )
    if not caps["allow_new"]:
        return {
            "approved": False, "qty": 0,
            "reason": f"blocked by risk caps -> {caps['reason']}",
            "flatten": caps["flatten"], "halt": caps["halt"],
        }

    # --- 2. Signal must be a real, actionable long/short with entry+stop. ---
    if not isinstance(signal, dict):
        return {"approved": False, "qty": 0, "reason": "signal is not a dict -> reject"}
    side = signal.get("signal")
    if side not in ("long", "short"):
        return {
            "approved": False, "qty": 0,
            "reason": f"signal '{side}' is not actionable (need long/short) -> reject",
        }
    entry = signal.get("entry")
    stop = signal.get("stop")
    target = signal.get("target")
    if _f(entry) is None or _f(stop) is None:
        return {
            "approved": False, "qty": 0,
            "reason": "signal missing a finite entry/stop -> reject",
        }

    # --- 3. Fixed-fractional sizing (also enforces R:R, leverage, risk_pct). ---
    sized = size_position(
        equity, entry, stop,
        risk_pct=risk_pct, max_risk_pct=max_risk_pct,
        leverage_cap=leverage_cap, target=target, min_rr=min_rr,
    )
    if not sized["approved"]:
        return {
            "approved": False, "qty": 0,
            "reason": f"sizing rejected -> {sized['reason']}",
        }

    # Approved. We pass through the GIVEN stop verbatim — never widened.
    return {
        "approved": True,
        "qty": sized["qty"],
        "side": side,
        "entry": _f(entry),
        "stop": _f(stop),                 # exactly the signal's stop, unmoved
        "target": _f(target),
        "risk_dollars": sized["risk_dollars"],
        "risk_per_unit": sized["risk_per_unit"],
        "leverage": sized["leverage"],
        "rr": sized.get("rr"),
        "reason": f"approved {side}: {sized['reason']}; caps: {caps['reason']}",
    }
