"""Deterministic "trade coach" — mines the audit ledger for ranked, actionable lessons.

Pure functions over a list of audit-row dicts (as returned by audit_tracker.export_rows()).
No I/O, never raises on bad input. Each lesson is a rule that fires only when the data
supports it, carrying concrete stats + a plain-language recommendation + a confidence note.

Design notes:
- Win-rate and MEDIAN pnl are trusted; $ SUMS are deliberately avoided because an aggressive
  martingale inflates a tiny tail of trades and makes sums misleading (verified on prod data).
- Outcome/timing/exit lessons work on historical (schema_version=0) rows. Entry-signal lessons
  require live (schema_version=1) rows and stay dormant (a 'pending' note) until enough accrue.
"""
from __future__ import annotations

import statistics
from typing import Any, Optional

WIN = "WIN"
LOSS = "LOSS"
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ── small stat helpers (all None-safe) ───────────────────────────────────────
def _labeled(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("settlement_status") in (WIN, LOSS)]


def _winrate(rows: list[dict]) -> Optional[float]:
    lab = _labeled(rows)
    if not lab:
        return None
    return round(100.0 * sum(1 for r in lab if r.get("settlement_status") == WIN) / len(lab), 1)


def _n(rows: list[dict]) -> int:
    return len(_labeled(rows))


def _median_pnl(rows: list[dict]) -> Optional[float]:
    vals = [float(r["realized_pnl"]) for r in rows if r.get("realized_pnl") is not None]
    return round(statistics.median(vals), 2) if vals else None


def _lesson(key: str, severity: str, title: str, stat: dict[str, Any],
            recommendation: str, confidence: str) -> dict[str, Any]:
    return {"key": key, "severity": severity, "title": title,
            "stat": stat, "recommendation": recommendation, "confidence": confidence}


# ── individual lessons (return a lesson dict or None if data doesn't support it) ──
def lesson_exit_discipline(rows: list[dict]) -> Optional[dict]:
    tp = [r for r in rows if r.get("exit_type") == "TP"]
    settle = [r for r in rows if r.get("exit_type") == "settle"]
    if _n(tp) < 10 or _n(settle) < 10:
        return None
    tp_wr, st_wr = _winrate(tp), _winrate(settle)
    if tp_wr is None or st_wr is None or (tp_wr - st_wr) < 15:
        return None
    # de-bias: among trades that DID reach a solid green peak, did holding to settle still lose?
    reached_held = [r for r in rows
                    if r.get("exit_type") == "settle" and (r.get("peak_unrealized_pct") or -1e9) >= 20]
    return _lesson(
        "exit_discipline", "critical",
        "משמעת יציאה: TP מנצח בהרבה על החזקה עד פקיעה",
        {"tp_n": _n(tp), "tp_winrate": tp_wr, "tp_median_pnl": _median_pnl(tp),
         "settle_n": _n(settle), "settle_winrate": st_wr, "settle_median_pnl": _median_pnl(settle),
         "reached20_then_held_n": _n(reached_held), "reached20_then_held_winrate": _winrate(reached_held)},
        "לאכוף TP/trailing-stop אוטומטי — אל תחזיק פוזיציה ירוקה עד הפקיעה.",
        "גבוה" if (_n(tp) + _n(settle)) >= 200 else "בינוני")


def lesson_green_turned_red(rows: list[dict], peak_thresh: float = 20.0) -> Optional[dict]:
    losses = [r for r in rows if r.get("settlement_status") == LOSS]
    if len(losses) < 20:
        return None
    was_green = [r for r in losses if (r.get("peak_unrealized_pct") or -1e9) >= peak_thresh]
    pct = round(100.0 * len(was_green) / len(losses), 1)
    if pct < 25:
        return None
    return _lesson(
        "green_turned_red", "critical",
        "ירוק שהפך אדום: רווחים שנמסרו בחזרה",
        {"loss_n": len(losses), "pct_losses_reached_peak": pct, "peak_threshold_pct": peak_thresh},
        "נעילת-רווח חלקית / trailing ברגע שעסקה עוברת סף ירוק.",
        "גבוה")


def lesson_side_edge(rows: list[dict]) -> Optional[dict]:
    by = {s: [r for r in rows if r.get("side") == s] for s in ("Up", "Down")}
    up_wr, dn_wr = _winrate(by["Up"]), _winrate(by["Down"])
    if up_wr is None or dn_wr is None or _n(by["Up"]) < 30 or _n(by["Down"]) < 30:
        return None
    if abs(dn_wr - up_wr) < 3:
        return None
    better = "Down" if dn_wr > up_wr else "Up"
    return _lesson(
        "side_edge", "medium",
        "אדג' לפי צד (win-rate, עמיד ל-sizing)",
        {"up_n": _n(by["Up"]), "up_winrate": up_wr, "up_median_pnl": _median_pnl(by["Up"]),
         "down_n": _n(by["Down"]), "down_winrate": dn_wr, "down_median_pnl": _median_pnl(by["Down"]),
         "better_side": better},
        f"צד {better} מנצח יותר. שים לב: סך ה-$ מוטה ע\"י martingale — הסתכל על win-rate/median, לא על הסכום.",
        "בינוני")


def lesson_martingale_risk(rows: list[dict]) -> Optional[dict]:
    losses = [float(r["realized_pnl"]) for r in rows
              if r.get("realized_pnl") is not None and float(r["realized_pnl"]) < 0]
    if len(losses) < 20:
        return None
    max_loss = min(losses)            # most negative
    med_loss = statistics.median(losses)
    if med_loss == 0 or max_loss > med_loss * 10:   # tail not dramatically worse than typical
        return None
    return _lesson(
        "martingale_risk", "critical",
        "סיכון martingale: זנב הפסדים קיצוני",
        {"max_single_loss": round(max_loss, 2), "median_loss": round(med_loss, 2),
         "max_to_median_ratio": round(max_loss / med_loss, 1),
         "n_losses_over_500": sum(1 for p in losses if p < -500)},
        "cap קשיח על גודל פוזיציה / loss-recovery multiplier + circuit-breaker אחרי N הכפלות רצופות.",
        "גבוה")


def lesson_drawdown(rows: list[dict]) -> Optional[dict]:
    # Only consider rows that actually have a trough reading, and use THAT as the denominator
    # (mixing in rows without a trough would under-report the dip rate).
    have_trough = [r for r in rows if r.get("trough_unrealized_pct") is not None]
    if len(have_trough) < 50:
        return None
    troughs = [float(r["trough_unrealized_pct"]) for r in have_trough]
    deep = [r for r in have_trough if float(r["trough_unrealized_pct"]) <= -50]
    return _lesson(
        "drawdown", "medium",
        "התנהגות drawdown (שוק בינארי מתנדנד)",
        {"median_trough_pct": round(statistics.median(troughs), 1),
         "pct_dipped_below_minus50": round(100.0 * len(deep) / len(have_trough), 1),
         "deep_dip_winrate": _winrate(deep)},
        "למקד משמעת בצד הרווח (TP/trailing). hard-stop רק בסף עמוק מאוד (<−80%) כדי לא לחתוך מנצחות.",
        "בינוני")


def lesson_signals(rows: list[dict]) -> Optional[dict]:
    """Entry-signal lessons — dormant until enough live (schema_version=1) rows accrue."""
    sv1 = [r for r in rows if r.get("schema_version") == 1]
    if _n(sv1) < 30:
        return _lesson(
            "signals_pending", "low",
            "לקחי אות-כניסה: ממתינים לעסקאות חיות",
            {"schema_v1_labeled": _n(sv1)},
            "הדלק את הבוט; ברגע שיצטברו ~30+ עסקאות עם ה-build החדש, יופקו לקחים אילו אותות (RSI/CLOB/סנטימנט) חוזים ניצחון.",
            "—")
    conf = [r for r in sv1 if r.get("signal_conflict") is True]
    noconf = [r for r in sv1 if r.get("signal_conflict") is False]
    cw, nw = _winrate(conf), _winrate(noconf)
    if cw is None or nw is None or _n(conf) < 10 or _n(noconf) < 10 or (nw - cw) < 5:
        return None
    return _lesson(
        "signal_conflict", "high",
        "כניסות נגד רוב הסיגנלים (signal_conflict) מפסידות יותר",
        {"conflict_n": _n(conf), "conflict_winrate": cw,
         "agree_n": _n(noconf), "agree_winrate": nw},
        "לחסום/להקטין כניסות שבהן הצד שנבחר מנוגד לרוב הסיגנלים.",
        "בינוני")


def config_risk_lessons(config: Optional[dict]) -> list[dict]:
    """Warn about a DANGEROUS CURRENT config (not history-derived). Each fires only on the
    actual risky value, so a safe config produces nothing. Helps a non-technical user SEE the
    risk in-app instead of only in a chat recommendation."""
    out: list[dict] = []
    if not config:
        return out
    try:
        lr_on = bool(config.get("loss_recovery_enabled"))
        cb_on = bool(config.get("circuit_breaker_enabled"))
        max_mult = float(config.get("loss_recovery_max_multiplier") or 0)
        tp = float(config.get("take_profit_pct") or 0)
        max_notional = float(config.get("max_notional_per_window_usd") or 0)

        if lr_on and max_mult > 100:
            out.append(_lesson(
                "config_martingale_cap", "critical",
                f"⚙️ הגדרה מסוכנת: מכפיל שחזור-הפסד עד {max_mult:.0f}× (ברירת-מחדל 10×)",
                {"loss_recovery_max_multiplier": max_mult, "loss_recovery_enabled": True},
                "הורד את loss_recovery_max_multiplier ל-2–3× בטאב אסטרטגיה — מכפיל גבוה = סיכון runaway של ה-martingale (בדיוק תקרית ה-85%−).",
                "ודאות"))
        if tp >= 80:
            out.append(_lesson(
                "config_tp_too_high", "high",
                f"⚙️ הגדרה: take_profit_pct={tp:.0f}% — TP כמעט לא נורה בחלון 5 דקות",
                {"take_profit_pct": tp},
                "הורד את take_profit_pct ל-15–20% כדי לממש רווח לפני שהוא נמחק (זה ה'ירוק שהפך אדום').",
                "ודאות"))
        if lr_on and max_notional >= 100000:
            # only a runaway risk when the martingale can escalate size; don't nag the default config
            out.append(_lesson(
                "config_no_notional_cap", "medium",
                "⚙️ ה-martingale פעיל ואין תקרת חשיפה אמיתית per-window",
                {"max_notional_per_window_usd": max_notional, "loss_recovery_enabled": True},
                "קבע max_notional_per_window_usd לתקרה אמיתית (פי 5–10 מההשקעה הבסיסית) כבלם גיבוי.",
                "בינוני"))
        if lr_on and not cb_on:
            out.append(_lesson(
                "config_cb_off", "high",
                "⚙️ ה-martingale פעיל אבל מפסק-הבטיחות (circuit-breaker) כבוי",
                {"loss_recovery_enabled": True, "circuit_breaker_enabled": False},
                "הפעל את מפסק-הבטיחות בטאב אסטרטגיה (עצור אחרי N הפסדים / בתקרת מכפיל) כבלם נגד runaway.",
                "בינוני"))
    except Exception as e:
        print(f"[trade_coach] config_risk_lessons failed: {e!r}", flush=True)
    return out


_LESSON_FNS = (
    lesson_exit_discipline,
    lesson_green_turned_red,
    lesson_side_edge,
    lesson_martingale_risk,
    lesson_drawdown,
    lesson_signals,
)


def compute_lessons(rows: list[dict], config: Optional[dict] = None) -> dict[str, Any]:
    """Run every lesson rule over the ledger rows (+ optional current-config risk checks) and
    return a ranked, JSON-safe result."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]  # honor the never-raises contract
    lessons: list[dict] = []
    for fn in _LESSON_FNS:
        try:
            out = fn(rows)
        except Exception as e:  # one bad rule must not sink the rest
            print(f"[trade_coach] {fn.__name__} failed: {e!r}", flush=True)
            out = None
        if out:
            lessons.append(out)
    lessons.extend(config_risk_lessons(config))  # current-config warnings (never raises)
    lessons.sort(key=lambda l: SEV_ORDER.get(l["severity"], 9))
    eras = {
        "total": len(rows),
        "labeled": _n(rows),
        "schema_v0": sum(1 for r in rows if r.get("schema_version") == 0),
        "schema_v1": sum(1 for r in rows if r.get("schema_version") == 1),
        "overall_winrate": _winrate(rows),
    }
    return {
        "note": "לקחי תוצאה/יציאה/תזמון זמינים מההיסטוריה; לקחי אות-כניסה דורשים עסקאות חיות (schema_version=1). "
                "סכומי $ מוטים ע\"י martingale — המאמן מתבסס על win-rate ו-median.",
        "eras": eras,
        "lessons": lessons,
    }
