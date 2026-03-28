"""
מנוע סיגנלים מאוחד — TA + CLOB Imbalance + היסטוריה + סנטימנט.
מחשב ציון ביטחון לUp/Down ומייצר המלצת כיוון.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from clob_imbalance import analyze_clob_imbalance
from history_tracker import get_win_rate_stats
from sentiment import compute_sentiment
from ta_signals import compute_ta_signals

# משקלות לכל קטגוריה (סכום = 1.0)
WEIGHTS = {
    "ta": 0.40,        # ניתוח טכני — הכי ישיר לטווח קצר
    "clob": 0.30,      # עומק ספר — smart money
    "history": 0.15,   # היסטוריית חלונות
    "sentiment": 0.15, # סנטימנט חיצוני
}

# מינימום ביטחון להמלצה — מתחת לזה = "neutral"
CONFIDENCE_THRESHOLD = 0.60

# Cache
_signals_cache: Optional[dict] = None
_signals_cache_ts: float = 0.0
_SIGNALS_TTL = 30.0  # 30 שניות


def _normalize_score(score: int, max_score: int) -> float:
    """ממיר ציון (±max) לטווח -1..+1."""
    if max_score <= 0:
        return 0.0
    return max(min(score / max_score, 1.0), -1.0)


def _score_to_confidence(normalized: float) -> tuple[float, float]:
    """
    ממיר ציון מנורמל (-1..+1) ל-(up_confidence, down_confidence) בטווח 0..1.
    """
    up_conf = (normalized + 1) / 2
    down_conf = 1.0 - up_conf
    return round(up_conf, 4), round(down_conf, 4)


async def compute_signals(
    up_book: Optional[dict] = None,
    down_book: Optional[dict] = None,
    window_sec: int = 300,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    מחשב את כל הסיגנלים ומחזיר:
      - up_confidence / down_confidence (0..1)
      - recommendation: 'Up' | 'Down' | 'neutral'
      - confidence_pct: אחוז ביטחון (50-100)
      - signals: רשימת סיגנלים מפורטים
      - sub: תוצאות כל קטגוריה בנפרד
    """
    global _signals_cache, _signals_cache_ts

    now = time.time()
    if (
        not force_refresh
        and _signals_cache
        and now - _signals_cache_ts < _SIGNALS_TTL
        # ספרים יכולים להשתנות, לכן לא cache-ים כשהם סופקו חיצונית
        and up_book is None
        and down_book is None
    ):
        return dict(_signals_cache)

    # הרצה מקבילית של TA + Sentiment
    ta_result, sentiment_result = await asyncio.gather(
        compute_ta_signals(),
        compute_sentiment(),
    )

    # CLOB Imbalance
    clob_result = analyze_clob_imbalance(up_book, down_book)

    # היסטוריה (סינכרוני — SQLite)
    history_result = get_win_rate_stats(window_sec)

    # --- חישוב ציונות מנורמלות ---

    # TA: score ± max_score
    ta_norm = 0.0
    if ta_result.get("available"):
        ta_norm = _normalize_score(
            ta_result.get("score", 0),
            ta_result.get("max_score", 4),
        )

    # CLOB: net_score כבר ב-[-1, 1] בעצם (אחרי scale)
    clob_norm = 0.0
    if clob_result.get("available"):
        net = clob_result.get("net_score", 0.0)
        # net_score הוא הפרש imbalances, כל אחד בטווח [-1,1]
        # לכן net ב-[-2,2] — נרמל ל-[-1,1]
        clob_norm = max(min(net, 1.0), -1.0)

    # History: up_rate 0..1 → -1..+1
    history_norm = 0.0
    if history_result.get("available"):
        hour_data = history_result.get("hour", {})
        overall_data = history_result.get("overall", {})
        # מעדיפים נתוני שעה אם יש מספיק
        if hour_data.get("total", 0) >= 5 and hour_data.get("up_rate") is not None:
            up_rate = float(hour_data["up_rate"])
        elif overall_data.get("up_rate") is not None:
            up_rate = float(overall_data["up_rate"])
        else:
            up_rate = 0.5
        history_norm = (up_rate - 0.5) * 2  # 0..1 → -1..+1

    # Sentiment: score ± max_score
    sentiment_norm = 0.0
    if sentiment_result.get("available"):
        sentiment_norm = _normalize_score(
            sentiment_result.get("score", 0),
            sentiment_result.get("max_score", 2),
        )

    # --- ציון משוקלל כולל ---
    weighted_score = (
        ta_norm * WEIGHTS["ta"]
        + clob_norm * WEIGHTS["clob"]
        + history_norm * WEIGHTS["history"]
        + sentiment_norm * WEIGHTS["sentiment"]
    )

    up_conf, down_conf = _score_to_confidence(weighted_score)

    # המלצה
    recommendation = "neutral"
    if up_conf >= CONFIDENCE_THRESHOLD:
        recommendation = "Up"
    elif down_conf >= CONFIDENCE_THRESHOLD:
        recommendation = "Down"

    confidence_pct = round(max(up_conf, down_conf) * 100, 1)

    # --- איחוד סיגנלים לתצוגה ---
    all_signals: list[dict] = []

    if ta_result.get("available"):
        all_signals.extend(ta_result.get("signals", []))
    if clob_result.get("available"):
        all_signals.extend(clob_result.get("signals", []))
    if sentiment_result.get("available"):
        all_signals.extend(sentiment_result.get("signals", []))

    # סיגנל היסטוריה
    if history_result.get("available"):
        hour_data = history_result.get("hour", {})
        overall_data = history_result.get("overall", {})
        total = history_result.get("total_windows", 0)
        h_total = hour_data.get("total", 0)
        cur_hour = history_result.get("current_hour_utc", 0)

        if h_total >= 5:
            up_rate = hour_data.get("up_rate") or 0.5
            if up_rate > 0.58:
                sig = "up"
                note = f"היסטוריה {cur_hour:02d}:00 UTC — Up ניצח {up_rate*100:.0f}% ({h_total} חלונות)"
            elif up_rate < 0.42:
                sig = "down"
                note = f"היסטוריה {cur_hour:02d}:00 UTC — Down ניצח {(1-up_rate)*100:.0f}% ({h_total} חלונות)"
            else:
                sig = "neutral"
                note = f"היסטוריה {cur_hour:02d}:00 UTC — מאוזן {up_rate*100:.0f}% Up ({h_total} חלונות)"
            all_signals.append({"name": "History", "signal": sig, "note": note})
        elif total >= 5:
            up_rate = overall_data.get("up_rate") or 0.5
            sig_name = "neutral" if abs(up_rate - 0.5) < 0.08 else ("up" if up_rate > 0.5 else "down")
            all_signals.append({
                "name": "History",
                "signal": sig_name,
                "note": f"היסטוריה כוללת — {total} חלונות, Up {up_rate*100:.0f}%",
            })
    elif not history_result.get("available"):
        all_signals.append({
            "name": "History",
            "signal": "neutral",
            "note": "היסטוריה — אין מספיק נתונים עדיין (יצטבר עם הזמן)",
        })

    result: dict[str, Any] = {
        "up_confidence": up_conf,
        "down_confidence": down_conf,
        "recommendation": recommendation,
        "confidence_pct": confidence_pct,
        "weighted_score": round(weighted_score, 4),
        "signals": all_signals,
        "sub": {
            "ta": ta_result,
            "clob": clob_result,
            "history": history_result,
            "sentiment": sentiment_result,
        },
        "weights": WEIGHTS,
        "threshold": CONFIDENCE_THRESHOLD,
        "ts": time.time(),
    }

    # שמירה ב-cache רק כשאין ספרים חיצוניים (generic refresh)
    if up_book is None and down_book is None:
        _signals_cache = dict(result)
        _signals_cache_ts = now

    return result
