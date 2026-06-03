"""
מדדי סנטימנט חיצוניים:
  - Binance Funding Rate (BTCUSDT perpetual)
  - Fear & Greed Index (alternative.me)
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx

BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"

# Cache
_funding_cache: Optional[dict] = None
_funding_ts: float = 0.0
# C-12: funding מתחשבן כל 8 שעות — TTL של 30 דק׳ מיושר לקצב האמיתי (היה 5 דק׳ ⇒ ~288 משיכות/יום).
# אסור לקצר: זה אות סנטימנט גס במשקל נמוך, לא מחיר/settlement.
_FUNDING_TTL = 1800.0  # 30 minutes (8h funding cadence)

_fg_cache: Optional[dict] = None
_fg_ts: float = 0.0
# C-11: Fear&Greed הוא נתון *יומי* — TTL של 6 שעות (היה שעה ⇒ ~24 משיכות/יום של אותו ערך).
_FG_TTL = 21600.0  # 6 hours (daily figure)


async def fetch_funding_rate() -> dict[str, Any]:
    """שיעור מימון BTCUSDT על חוזה עתידי רציף."""
    global _funding_cache, _funding_ts
    now = time.time()
    if _funding_cache and now - _funding_ts < _FUNDING_TTL:
        return dict(_funding_cache)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                BINANCE_FUNDING_URL,
                params={"symbol": "BTCUSDT", "limit": 1},
                timeout=8.0,
            )
            r.raise_for_status()
            data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            rate = float(data[0].get("fundingRate", 0))
            rate_pct = rate * 100
            if rate > 0.0005:
                signal = "down"
                note = f"שיעור מימון גבוה ({rate_pct:.4f}%) — over-leveraged longs, ירידה סביר"
            elif rate < -0.0005:
                signal = "up"
                note = f"שיעור מימון שלילי ({rate_pct:.4f}%) — over-leveraged shorts, עלייה סביר"
            else:
                signal = "neutral"
                note = f"שיעור מימון ניטרלי ({rate_pct:.4f}%)"
            result: dict[str, Any] = {
                "available": True,
                "rate_pct": round(rate_pct, 4),
                "signal": signal,
                "note": note,
            }
            _funding_cache = result
            _funding_ts = now
            return result
    except Exception as e:
        return {"available": False, "error": str(e)}
    return {"available": False, "error": "no data"}


async def fetch_fear_greed() -> dict[str, Any]:
    """Fear & Greed Index (0=extreme fear, 100=extreme greed)."""
    global _fg_cache, _fg_ts
    now = time.time()
    if _fg_cache and now - _fg_ts < _FG_TTL:
        return dict(_fg_cache)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(FEAR_GREED_URL, timeout=8.0)
            r.raise_for_status()
            data = r.json()
        fg_data = data.get("data", [])
        if fg_data:
            value = int(fg_data[0]["value"])
            classification = fg_data[0].get("value_classification", "")
            if value >= 75:
                signal = "down"
                note = f"Fear & Greed: {value} ({classification}) — חמדנות קיצונית, ירידה אפשרית"
            elif value <= 25:
                signal = "up"
                note = f"Fear & Greed: {value} ({classification}) — פחד קיצוני, עלייה אפשרית"
            else:
                signal = "neutral"
                note = f"Fear & Greed: {value} ({classification})"
            result = {
                "available": True,
                "value": value,
                "classification": classification,
                "signal": signal,
                "note": note,
            }
            _fg_cache = result
            _fg_ts = now
            return result
    except Exception as e:
        return {"available": False, "error": str(e)}
    return {"available": False, "error": "no data"}


async def compute_sentiment() -> dict[str, Any]:
    """מאחד funding rate + Fear & Greed לציון סנטימנט."""
    import asyncio
    funding, fear_greed = await asyncio.gather(
        fetch_funding_rate(),
        fetch_fear_greed(),
    )

    score = 0
    signals: list[dict] = []

    if funding.get("available"):
        sig = funding.get("signal", "neutral")
        if sig == "up":
            score += 1
        elif sig == "down":
            score -= 1
        signals.append({"name": "Funding", "signal": sig, "note": funding.get("note", "")})

    if fear_greed.get("available"):
        sig = fear_greed.get("signal", "neutral")
        if sig == "up":
            score += 1
        elif sig == "down":
            score -= 1
        signals.append({"name": "F&G", "signal": sig, "note": fear_greed.get("note", "")})

    return {
        "available": True,
        "funding": funding,
        "fear_greed": fear_greed,
        "score": score,
        "max_score": 2,
        "signals": signals,
    }
