"""Circuit-breaker decision logic — pure, I/O-free, never raises.

The breaker is OPT-IN and OFF by default: with `enabled=False` (or all thresholds left at
their disabled sentinels) `should_halt` always returns None, so wiring it in changes nothing
until the user turns it on. It only ever makes the bot *safer* (stop opening new positions).

`should_halt` returns a short human reason string when the bot should halt new entries, or
None otherwise. It is the single source of truth; callers decide what to DO with a trip
(block entries / set mode off) and never trap an already-open position.
"""
from __future__ import annotations

from typing import Optional


def should_halt(
    *,
    enabled: bool,
    streak: int,
    multiplier: float,
    cap: float,
    equity: Optional[float],
    baseline: Optional[float],
    max_consecutive_losses: int = 0,      # 0 = this condition disabled
    halt_at_cap: bool = False,
    equity_floor_pct: float = 0.0,        # 0 = this condition disabled
) -> Optional[str]:
    """Return a trip reason if any enabled breaker condition is met, else None.

    Conditions (each independently gated, so leaving a threshold at its sentinel disables it):
      - consecutive losses: streak >= max_consecutive_losses (when max_consecutive_losses > 0)
      - hit cap:            multiplier >= cap                (when halt_at_cap and cap > 1.0)
      - equity floor:       equity < baseline * equity_floor_pct/100  (when equity_floor_pct > 0)
    """
    try:
        if not enabled:
            return None

        n = int(max_consecutive_losses or 0)
        if n > 0 and int(streak or 0) >= n:
            return f"{int(streak)} consecutive losses ≥ {n}"

        if halt_at_cap and float(cap or 0) > 1.0 and float(multiplier or 0) >= float(cap):
            return f"loss-recovery multiplier {float(multiplier):.2f}× hit cap {float(cap):.2f}×"

        floor_pct = float(equity_floor_pct or 0)
        if floor_pct > 0 and baseline and float(baseline) > 0 and equity is not None:
            floor = float(baseline) * (floor_pct / 100.0)
            if float(equity) < floor:
                return (f"equity {float(equity):.2f} below floor {floor:.2f} "
                        f"({floor_pct:.0f}% of baseline {float(baseline):.2f})")

        return None
    except Exception:
        # A breaker bug must never crash the trading loop, and must never falsely halt.
        return None
