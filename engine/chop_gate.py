"""Chop-Armed Follow-the-Winner — pure decision logic (I/O-free, never raises).

A "chop" = N strictly-alternating 5-min window outcomes (each opposite the previous),
e.g. N=4 → 🔴🟢🔴🟢. When a chop is detected the strategy ARMS a Follow-Last-Winner
campaign with bounded martingale (sizing reuses loss_recovery.py); the campaign ENDS
when a loss occurs while already at the max multiplier — back to waiting for the next
chop. This module holds only the two pure predicates; wiring lives on StrategyRunner.

Mirrors circuit_breaker.py: never raises, so a bug here can never crash the trade loop.
"""
from __future__ import annotations

from typing import Any, Optional

_SIDES = ("Up", "Down")


def is_chop(sides: Any, n: Any) -> bool:
    """True iff the first `n` of `sides` (MOST-RECENT-FIRST) strictly alternate.

    `sides` is the list returned by history_tracker.get_last_window_winners (each
    "Up"/"Down", newest first). Windows beyond the first `n` are ignored. A chop needs
    at least 2 windows (n>=2). Any malformed input → False (never raises).
    """
    try:
        n = int(n)
        if n < 2 or not isinstance(sides, (list, tuple)) or len(sides) < n:
            return False
        window = sides[:n]
        if any(s not in _SIDES for s in window):
            return False
        # strictly alternating: each differs from the previous
        return all(window[i] != window[i - 1] for i in range(1, n))
    except Exception:
        return False


def campaign_should_end(*, multiplier: Any, cap: Any, had_loss: Any) -> bool:
    """True iff a loss just occurred while the martingale is already at (or above) the cap.

    That's the "recovery exhausted" condition: keep doubling on every loss up to `cap`,
    and only when a loss lands at the cap does the campaign end (→ wait for next chop).
    With cap==1.0 (no doubling) any loss ends the campaign immediately. Never raises.
    """
    try:
        if not had_loss:
            return False
        return float(multiplier) >= float(cap)
    except Exception:
        return False
