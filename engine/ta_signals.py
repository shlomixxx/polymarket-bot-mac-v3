"""
ניתוח טכני של BTC: RSI(14), EMA9, EMA21, ATR(14), מומנטום.
מבוסס על נרות 1m מ-Binance BTCUSDT.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


async def fetch_btc_klines(interval: str = "1m", limit: int = 60) -> list[dict]:
    """מושך נרות BTCUSDT מ-Binance."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            BINANCE_KLINES,
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=10.0,
        )
        r.raise_for_status()
        raw = r.json()
    return [
        {
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in raw
    ]


def _ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """RSI(period) על סדרת מחירי סגירה."""
    if len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_atr(candles: list[dict], period: int = 14) -> Optional[float]:
    """ATR(period) — תנודתיות ממוצעת."""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)
    atr = sum(true_ranges[:period]) / period
    for i in range(period, len(true_ranges)):
        atr = (atr * (period - 1) + true_ranges[i]) / period
    return atr


async def compute_ta_signals() -> dict[str, Any]:
    """
    מחשב אינדיקטורים טכניים לBTCUSDT.
    מחזיר RSI, EMA9/21, ATR14, מומנטום %, ציון כיווני.
    """
    try:
        candles = await fetch_btc_klines(interval="1m", limit=60)
    except Exception as e:
        return {"error": str(e), "available": False}

    if not candles:
        return {"error": "no candles", "available": False}

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    rsi = compute_rsi(closes)
    atr = compute_atr(candles)

    ema9_series = _ema(closes, 9)
    ema21_series = _ema(closes, 21)
    ema9 = ema9_series[-1] if ema9_series else None
    ema21 = ema21_series[-1] if ema21_series else None

    # מומנטום 3 דקות
    momentum_3m: Optional[float] = None
    if len(closes) >= 4 and closes[-4] > 0:
        momentum_3m = (current_price - closes[-4]) / closes[-4] * 100

    # מומנטום 5 דקות
    momentum_5m: Optional[float] = None
    if len(closes) >= 6 and closes[-6] > 0:
        momentum_5m = (current_price - closes[-6]) / closes[-6] * 100

    score = 0
    signals: list[dict] = []

    # RSI
    if rsi is not None:
        if rsi > 55:
            score += 1
            signals.append({"name": "RSI", "value": round(rsi, 1), "signal": "up",
                            "note": f"RSI {rsi:.1f} > 55 — מומנטום עולה"})
        elif rsi < 45:
            score -= 1
            signals.append({"name": "RSI", "value": round(rsi, 1), "signal": "down",
                            "note": f"RSI {rsi:.1f} < 45 — מומנטום יורד"})
        else:
            signals.append({"name": "RSI", "value": round(rsi, 1), "signal": "neutral",
                            "note": f"RSI {rsi:.1f} — ניטרלי (45–55)"})

    # EMA Crossover
    if ema9 is not None and ema21 is not None:
        diff = ema9 - ema21
        if diff > 0:
            score += 1
            signals.append({"name": "EMA", "value": round(diff, 2), "signal": "up",
                            "note": f"EMA9 ({ema9:.0f}) > EMA21 ({ema21:.0f}) — טרנד עולה"})
        else:
            score -= 1
            signals.append({"name": "EMA", "value": round(diff, 2), "signal": "down",
                            "note": f"EMA9 ({ema9:.0f}) < EMA21 ({ema21:.0f}) — טרנד יורד"})

    # מומנטום 3m
    if momentum_3m is not None:
        if momentum_3m > 0.05:
            score += 1
            signals.append({"name": "MOM3m", "value": round(momentum_3m, 3), "signal": "up",
                            "note": f"מומנטום 3m: +{momentum_3m:.3f}% — עולה"})
        elif momentum_3m < -0.05:
            score -= 1
            signals.append({"name": "MOM3m", "value": round(momentum_3m, 3), "signal": "down",
                            "note": f"מומנטום 3m: {momentum_3m:.3f}% — יורד"})
        else:
            signals.append({"name": "MOM3m", "value": round(momentum_3m, 3), "signal": "neutral",
                            "note": f"מומנטום 3m: {momentum_3m:.3f}% — ניטרלי"})

    # מומנטום 5m
    if momentum_5m is not None:
        if momentum_5m > 0.08:
            score += 1
            signals.append({"name": "MOM5m", "value": round(momentum_5m, 3), "signal": "up",
                            "note": f"מומנטום 5m: +{momentum_5m:.3f}%"})
        elif momentum_5m < -0.08:
            score -= 1
            signals.append({"name": "MOM5m", "value": round(momentum_5m, 3), "signal": "down",
                            "note": f"מומנטום 5m: {momentum_5m:.3f}%"})
        else:
            signals.append({"name": "MOM5m", "value": round(momentum_5m, 3), "signal": "neutral",
                            "note": f"מומנטום 5m: {momentum_5m:.3f}% — ניטרלי"})

    return {
        "available": True,
        "current_price": current_price,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "ema9": round(ema9, 2) if ema9 is not None else None,
        "ema21": round(ema21, 2) if ema21 is not None else None,
        "atr": round(atr, 2) if atr is not None else None,
        "momentum_3m_pct": round(momentum_3m, 4) if momentum_3m is not None else None,
        "momentum_5m_pct": round(momentum_5m, 4) if momentum_5m is not None else None,
        "score": score,
        "max_score": 4,
        "signals": signals,
        "ts": time.time(),
    }
