"""binance_equity.py — persistent equity tracker for the account-level loss caps.

The manual Binance cockpit enforces per-trade risk in risk_engine.gate_order, but
the ACCOUNT-LEVEL backstops (daily -3% flatten, global -10% halt) need a running
P&L feed: today's P&L vs the day's starting equity, and drawdown from the
all-time equity peak. This module is that feed.

It is deliberately tiny and fail-safe:
  * the MATH is a pure function (`compute_state`) — unit-testable with no files;
  * the persisted state (peak_equity + day_start_equity + day_start_date) lives in
    one small JSON under DATA_ROOT, written atomically (tmp + os.replace);
  * `update_and_compute` NEVER raises — on ANY error it returns a SAFE 0/0 result
    (0 means "caps not tripped", preserving the cockpit's prior behaviour). A
    glitch must never wrongly flatten or halt real money.

Convention: day_pnl_pct and peak_drawdown_pct are PERCENTS. peak_drawdown_pct is
<= 0 (we're always at-or-below the peak). risk_engine.check_caps consumes both.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

# Safe, inert result: 0/0 means "no cap tripped" -> keeps the cockpit's behaviour.
_INERT: dict[str, float] = {
    "day_pnl_pct": 0.0,
    "peak_drawdown_pct": 0.0,
    "peak_equity": 0.0,
    "day_start_equity": 0.0,
}


def _f(x: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Pure, never raises."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return v


def compute_state(
    current_equity: float,
    prior: Optional[dict[str, Any]],
    *,
    utc_date: str,
) -> dict[str, Any]:
    """PURE math: given current equity, the prior persisted state (or None on the
    first call), and today's UTC date, return the NEXT state + the two cap pcts.

    Returns a dict with: day_pnl_pct, peak_drawdown_pct, peak_equity,
    day_start_equity, day_start_date. Never raises — on garbage input returns the
    inert 0/0 result (re-anchored to current_equity when that's usable).

    Rules:
      * NEW utc day (utc_date != prior day_start_date)  -> day_start_equity = equity.
      * peak_equity = max(prior peak, equity)            -> peak only ever rises.
      * day_pnl_pct       = (equity - day_start)/day_start * 100.
      * peak_drawdown_pct = (equity - peak)/peak * 100   (<= 0).
      * divide-by-zero / first call / bad prior          -> initialise, 0/0.
    """
    eq = _f(current_equity)
    if eq is None:
        # Can't compute anything meaningful; stay inert.
        return dict(_INERT)

    prior = prior if isinstance(prior, dict) else {}
    prior_date = prior.get("day_start_date")
    prior_day_start = _f(prior.get("day_start_equity"))
    prior_peak = _f(prior.get("peak_equity"))

    # Day anchor: reset on a new UTC day, on first call, or if the stored anchor
    # is missing/non-positive (can't divide by it).
    if prior_date != utc_date or prior_day_start is None or prior_day_start <= 0:
        day_start = eq
    else:
        day_start = prior_day_start

    # Peak only ever rises; seed from current equity on first/garbled state.
    if prior_peak is None or prior_peak <= 0:
        peak = eq
    else:
        peak = max(prior_peak, eq)

    day_pnl_pct = (eq - day_start) / day_start * 100.0 if day_start > 0 else 0.0
    peak_drawdown_pct = (eq - peak) / peak * 100.0 if peak > 0 else 0.0

    return {
        "day_pnl_pct": day_pnl_pct,
        "peak_drawdown_pct": peak_drawdown_pct,
        "peak_equity": peak,
        "day_start_equity": day_start,
        "day_start_date": utc_date,
    }


def _read_state(path: Path) -> Optional[dict[str, Any]]:
    """Load the persisted state; None if absent/unreadable. Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # corrupt JSON, perms, etc. -> treat as first call.
        _log.warning("binance_equity: could not read %s: %s", path, exc)
        return None


def _write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    """Atomic write (tmp in same dir + os.replace). Never raises (best-effort
    persistence — a failed write must not break the read path)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, ensure_ascii=False, indent=2))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, str(path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:
        _log.warning("binance_equity: could not persist %s: %s", path, exc)


def update_and_compute(
    current_equity: float,
    *,
    now_ts: float,
    utc_date: str,
    state_path: Path,
) -> dict[str, float]:
    """Load prior state, fold in `current_equity`, persist, and return the cap
    inputs: {day_pnl_pct, peak_drawdown_pct, peak_equity, day_start_equity}.

    NEVER raises. On ANY error returns the inert 0/0 result so a glitch cannot
    wrongly trip a cap (0 == "not tripped"). `now_ts` is stamped into the persisted
    state for observability; `utc_date` drives the day reset (caller computes both
    via time.time()/time.gmtime — no JS Date).
    """
    try:
        path = Path(state_path)
        prior = _read_state(path)
        nxt = compute_state(current_equity, prior, utc_date=utc_date)
        # Stamp the update time for audit/observability; not used in the math.
        to_persist = dict(nxt)
        to_persist["updated_ts"] = float(now_ts)
        _write_state_atomic(path, to_persist)
        return {
            "day_pnl_pct": float(nxt["day_pnl_pct"]),
            "peak_drawdown_pct": float(nxt["peak_drawdown_pct"]),
            "peak_equity": float(nxt["peak_equity"]),
            "day_start_equity": float(nxt["day_start_equity"]),
        }
    except Exception as exc:  # absolute fail-safe — read path must never raise.
        _log.warning("binance_equity.update_and_compute failed (inert 0/0): %s", exc)
        return dict(_INERT)
