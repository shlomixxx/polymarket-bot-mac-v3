"""
עדכון מצב שחזור אחרי הפסד (מכפיל השקעה) לפי עסקאות פירוק.
"""
from __future__ import annotations

from typing import Any

from demo_engine import DemoState


def apply_loss_recovery_from_settlements(
    state: DemoState,
    *,
    enabled: bool,
    step_pct: float,
    every_n_losses: int,
    max_multiplier: float,
    settlement_trades: list[dict[str, Any]],
) -> list[str]:
    """מעדכן loss_recovery_streak / loss_recovery_multiplier לפי realized_pnl של עסקאות הפירוק.

    - realized_pnl > 0: איפוס (רווח / פירוק מנצח).
    - realized_pnl < 0: streak++, ובכל N הפסדים הרצופים (כאשר streak % N == 0)
      מכפילים את המכפיל ב-(1 + step_pct/100), עד תקרת max_multiplier.
      דוגמה: step_pct=100 ו-N=1 ⇒ הכפלה ×2 אחרי כל פירוק מופסד.

    מחזיר שורות טקסט ליומן האסטרטגיה (ריק אם כבוי או בלי עסקאות רלוונטיות).
    """
    lines: list[str] = []
    if not enabled or not settlement_trades:
        return lines

    n_period = max(1, int(every_n_losses))
    # תקרת-ברזל מוחלטת על המכפיל המאוחסן: גם אם המשתמש/ה-config מתיר max_multiplier=100000,
    # המכפיל לעולם לא יצטבר מעבר ל-HARD_MAX_LOSS_RECOVERY_MULT (incident 2026-06-15: 1525×).
    # ה-sizing ב-strategy_runner גם הוא חוסם בתקרה הזו — זו שכבה שנייה כדי שה-state עצמו לא יתנפח.
    try:
        from strategy_runner import HARD_MAX_LOSS_RECOVERY_MULT as _HARD_CAP
    except Exception:
        _HARD_CAP = 3.0  # keep in sync with strategy_runner.HARD_MAX_LOSS_RECOVERY_MULT
    cap = min(max(1.0, float(max_multiplier)), float(_HARD_CAP))
    step = max(0.0, float(step_pct))
    factor = 1.0 + step / 100.0 if step > 0 else 1.0

    for t in settlement_trades:
        typ = str(t.get("type") or "")
        # תוצאה לא-ידועה (כשל זמני במשיכת מחיר BTC / חסר epoch) אינה הפסד אמיתי —
        # אסור שתסלים או תאפס את מכפיל השחזור. זו בדיוק התקלה שניפחה את המכפיל
        # ל-9537× ורוקנה את החשבון: כשלי מחיר נספרו כהפסדים מלאים והכפילו את ההימור.
        # מדלגים: המכפיל נשאר ללא שינוי עד תוצאה ודאית (Win/Loss).
        if typ == "SETTLE_UNKNOWN" or t.get("settlement_error"):
            continue
        rpnl = t.get("realized_pnl")
        if rpnl is None:
            continue
        try:
            r = float(rpnl)
        except (TypeError, ValueError):
            continue
        sid = str(t.get("session_id") or "")[:8] if t.get("session_id") else "—"
        if r > 0:
            state.loss_recovery_streak = 0
            state.loss_recovery_multiplier = 1.0
            lines.append(
                f"שחזור הפסד: פירוק מנצח ({typ}, session {sid}, PnL +{r:.2f}$) — איפוס: מכפיל 1.00×, רצף 0"
            )
        elif r < 0:
            mult_before = state.loss_recovery_multiplier
            streak_before = state.loss_recovery_streak
            state.loss_recovery_streak += 1
            bumped = False
            if factor > 1.0 and state.loss_recovery_streak > 0:
                if state.loss_recovery_streak % n_period == 0:
                    new_m = min(cap, state.loss_recovery_multiplier * factor)
                    bumped = new_m != mult_before
                    state.loss_recovery_multiplier = new_m
            msg = (
                f"שחזור הפסד: פירוק בהפסד ({typ}, session {sid}, PnL {r:.2f}$) — "
                f"רצף {streak_before}→{state.loss_recovery_streak}, "
                f"מכפיל {mult_before:.2f}×→{state.loss_recovery_multiplier:.2f}×"
            )
            if bumped:
                msg += f" (צעד +{step:.0f}% כל {n_period} הפסדים, תקרה {cap:.1f}×)"
            elif step <= 0:
                msg += " (צעד 0% — רק ספירת רצף)"
            elif n_period > 1 and state.loss_recovery_streak % n_period != 0:
                rem = n_period - (state.loss_recovery_streak % n_period)
                msg += f" (ממתין לצעד מכפיל — עוד {rem} הפסדים עד צעד)"
            lines.append(msg)
    return lines
