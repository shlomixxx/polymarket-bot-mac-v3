"""
ניתוח טכני של BTC: RSI(14), EMA9, EMA21, ATR(14), מומנטום.
מבוסס על נרות 1m מ-Binance BTCUSDT.
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

import httpx

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# קאש 15s ל-fetch_btc_klines: הנרות הם 1m ולכן 15s סטליות לא משפיעה על אינדיקטורים,
# אבל היא מבטלת קריאות חוזרות מצד /api/signals שנקרא ב-2-3s ע"י שני frontends במקביל.
_KLINES_CACHE: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_KLINES_CACHE_TTL_SEC = 15.0


async def fetch_btc_klines(interval: str = "1m", limit: int = 250) -> list[dict]:
    """מושך נרות BTCUSDT מ-Binance, עם קאש 15s לפי (interval, limit)."""
    key = (interval, limit)
    now = time.time()
    cached = _KLINES_CACHE.get(key)
    if cached is not None and (now - cached[0]) <= _KLINES_CACHE_TTL_SEC:
        return cached[1]
    async with httpx.AsyncClient() as client:
        r = await client.get(
            BINANCE_KLINES,
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=10.0,
        )
        r.raise_for_status()
        raw = r.json()
    result = [
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
    _KLINES_CACHE[key] = (time.time(), result)
    return result


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


# ---------------------------------------------------------------------------
# Pure recording-only feature helpers (no network, never raise on their own).
# Used to enrich the audit ledger with ML-trading features. They DO NOT feed
# the trading score/signals — they are added under return["features"] only.
# ---------------------------------------------------------------------------


def _sig6(x: Optional[float]) -> Optional[float]:
    """Round to ~6 significant figures (keeps the recorded blob small)."""
    if x is None:
        return None
    try:
        if not math.isfinite(x):
            return None
        if x == 0:
            return 0.0
        from math import floor, log10

        digits = 6 - int(floor(log10(abs(x)))) - 1
        return round(x, digits)
    except Exception:
        return None


def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """MACD line, signal line, histogram (raw price units).

    Returns (macd, signal_line, hist). Each is None if not enough data.
    The MACD line is EMA(fast) - EMA(slow) aligned on the same bars; the
    signal line is EMA(signal) of the MACD line.
    """
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    if not ema_fast or not ema_slow:
        return None, None, None
    # Align both EMA series to the same (shorter, slower) right edge.
    n = min(len(ema_fast), len(ema_slow))
    ema_fast_al = ema_fast[-n:]
    ema_slow_al = ema_slow[-n:]
    macd_line = [f - s for f, s in zip(ema_fast_al, ema_slow_al)]
    if len(macd_line) < signal:
        return None, None, None
    signal_series = _ema(macd_line, signal)
    if not signal_series:
        return None, None, None
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    hist = macd_val - signal_val
    return macd_val, signal_val, hist


def compute_stochastic(
    candles: list[dict],
    period: int = 14,
    smooth_d: int = 3,
) -> tuple[Optional[float], Optional[float]]:
    """Stochastic oscillator %K(period) and %D (SMA(smooth_d) of %K).

    %K = 100 * (close - lowest_low) / (highest_high - lowest_low).
    Returns (k, d); d is None if fewer than smooth_d %K values exist.
    """
    if len(candles) < period:
        return None, None
    k_values: list[float] = []
    # Need enough bars so we can produce smooth_d %K points for %D.
    needed = period + smooth_d - 1
    start = max(period, len(candles) - (smooth_d - 1))
    # Build %K for the last smooth_d positions (or fewer if not available).
    for end in range(start, len(candles) + 1):
        window = candles[end - period:end]
        highs = [c["high"] for c in window]
        lows = [c["low"] for c in window]
        close = window[-1]["close"]
        hh = max(highs)
        ll = min(lows)
        rng = hh - ll
        if rng == 0:
            k_values.append(50.0)
        else:
            k_values.append(100.0 * (close - ll) / rng)
    k = k_values[-1] if k_values else None
    d: Optional[float] = None
    if len(candles) >= needed and len(k_values) >= smooth_d:
        d = sum(k_values[-smooth_d:]) / smooth_d
    return k, d


def compute_bollinger(
    closes: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[Optional[float], Optional[float]]:
    """Bollinger Bands (period, num_std) -> (pct_b, bandwidth).

    pct_b = (close - lower) / (upper - lower); bandwidth = (upper - lower)/mid.
    Returns (None, None) if not enough data.
    """
    if len(closes) < period:
        return None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((c - mid) ** 2 for c in window) / period
    std = math.sqrt(variance)
    upper = mid + num_std * std
    lower = mid - num_std * std
    close = closes[-1]
    band = upper - lower
    pct_b = (close - lower) / band if band != 0 else 0.5
    bandwidth = band / mid if mid != 0 else 0.0
    return pct_b, bandwidth


def log_return(closes: list[float], lag: int) -> Optional[float]:
    """ln(close[-1] / close[-1-lag]); None if not enough data or non-positive."""
    if lag <= 0 or len(closes) < lag + 1:
        return None
    a = closes[-1]
    b = closes[-1 - lag]
    if a <= 0 or b <= 0:
        return None
    return math.log(a / b)


def realized_vol(closes: list[float], n: int) -> Optional[float]:
    """sqrt(sum of squared 1m log returns over the last n returns)."""
    if n <= 0 or len(closes) < n + 1:
        return None
    total = 0.0
    for i in range(len(closes) - n, len(closes)):
        a = closes[i]
        b = closes[i - 1]
        if a <= 0 or b <= 0:
            return None
        r = math.log(a / b)
        total += r * r
    return math.sqrt(total)


def compute_obv(closes: list[float], volumes: list[float]) -> Optional[float]:
    """On-balance volume over the provided window (cumulative, starts at 0)."""
    if len(closes) < 2 or len(volumes) != len(closes):
        return None
    obv = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
    return obv


def obv_slope(closes: list[float], volumes: list[float], lookback: int = 10) -> Optional[float]:
    """Recent slope of OBV: OBV(now) - OBV(lookback bars ago), per-bar OBV series."""
    if len(closes) < lookback + 2 or len(volumes) != len(closes):
        return None
    obv = 0.0
    series = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
        series.append(obv)
    return (series[-1] - series[-1 - lookback]) / lookback


def volume_zscore(volumes: list[float], window: int = 20) -> Optional[float]:
    """z-score of the latest volume vs the previous `window` bars."""
    if len(volumes) < window + 1:
        return None
    ref = volumes[-1 - window:-1]
    mean = sum(ref) / len(ref)
    var = sum((v - mean) ** 2 for v in ref) / len(ref)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (volumes[-1] - mean) / std


def _build_features(candles: list[dict]) -> dict[str, Any]:
    """Compute the recording-only ML features. Pure math, never raises.

    Any individual indicator that lacks data is set to None.
    """
    closes = [c["close"] for c in candles]
    volumes = [c.get("volume", 0.0) for c in candles]
    close = closes[-1] if closes else None

    feats: dict[str, Any] = {}

    def pct_of_close(v: Optional[float]) -> Optional[float]:
        if v is None or not close:
            return None
        return v / close * 100

    # --- MACD (12,26,9) as % of price ---
    macd, macd_sig, macd_hist = compute_macd(closes)
    feats["macd_pct"] = _sig6(pct_of_close(macd))
    feats["macd_signal_pct"] = _sig6(pct_of_close(macd_sig))
    feats["macd_hist_pct"] = _sig6(pct_of_close(macd_hist))

    # --- RSI multi-period (rsi14 is top-level) ---
    feats["rsi7"] = _sig6(compute_rsi(closes, 7))
    feats["rsi30"] = _sig6(compute_rsi(closes, 30))

    # --- Stochastic ---
    stoch_k, stoch_d = compute_stochastic(candles, 14, 3)
    feats["stoch_k"] = _sig6(stoch_k)
    feats["stoch_d"] = _sig6(stoch_d)
    stoch_k_30, _ = compute_stochastic(candles, 30, 3)
    feats["stoch_k_30"] = _sig6(stoch_k_30)

    # --- Bollinger (20,2) ---
    bb_pct_b, bb_bw = compute_bollinger(closes, 20, 2.0)
    feats["bb_pct_b"] = _sig6(bb_pct_b)
    feats["bb_bandwidth"] = _sig6(bb_bw)

    # --- Multi-lag log returns ---
    feats["ret_1m"] = _sig6(log_return(closes, 1))
    feats["ret_2m"] = _sig6(log_return(closes, 2))
    feats["ret_3m"] = _sig6(log_return(closes, 3))
    feats["ret_5m"] = _sig6(log_return(closes, 5))
    feats["ret_10m"] = _sig6(log_return(closes, 10))
    feats["ret_15m"] = _sig6(log_return(closes, 15))

    # --- Realized volatility ---
    feats["rv_5"] = _sig6(realized_vol(closes, 5))
    feats["rv_15"] = _sig6(realized_vol(closes, 15))
    feats["rv_30"] = _sig6(realized_vol(closes, 30))

    # --- EMA structure ---
    ema9_series = _ema(closes, 9)
    ema21_series = _ema(closes, 21)
    ema50_series = _ema(closes, 50)
    ema200_series = _ema(closes, 200)
    ema9 = ema9_series[-1] if ema9_series else None
    ema21 = ema21_series[-1] if ema21_series else None
    ema50 = ema50_series[-1] if ema50_series else None
    ema200 = ema200_series[-1] if ema200_series else None
    feats["ema50"] = _sig6(ema50)
    feats["ema200"] = _sig6(ema200)
    feats["ema9_21_ratio"] = (
        _sig6(ema9 / ema21 - 1) if (ema9 is not None and ema21) else None
    )
    feats["price_vs_ema21_pct"] = (
        _sig6((close - ema21) / ema21 * 100) if (close is not None and ema21) else None
    )

    # --- Volume features ---
    latest_vol = volumes[-1] if volumes else None
    feats["volume"] = _sig6(latest_vol)
    feats["volume_z"] = _sig6(volume_zscore(volumes, 20))
    feats["obv"] = _sig6(compute_obv(closes, volumes))
    feats["obv_slope"] = _sig6(obv_slope(closes, volumes, 10))

    # --- ATR % ---
    atr = compute_atr(candles)
    feats["atr_pct"] = _sig6(atr / close * 100) if (atr is not None and close) else None

    return feats


def _build_raw_candles(candles: list[dict], n: int = 60) -> list[list[float]]:
    """Last n candles as compact [open, high, low, close, volume] arrays."""
    tail = candles[-n:] if len(candles) > n else candles
    out: list[list[float]] = []
    for c in tail:
        out.append([
            _sig6(c.get("open")),
            _sig6(c.get("high")),
            _sig6(c.get("low")),
            _sig6(c.get("close")),
            _sig6(c.get("volume")),
        ])
    return out


async def compute_ta_signals() -> dict[str, Any]:
    """
    מחשב אינדיקטורים טכניים לBTCUSDT.
    מחזיר RSI, EMA9/21, ATR14, מומנטום %, ציון כיווני.
    """
    try:
        candles = await fetch_btc_klines(interval="1m", limit=250)
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

    # --- Recording-only enrichment (DATA-ONLY: does not affect score/signals).
    # Wrapped so any failure leaves features partial/None but never breaks the
    # advisory trading signal that the engine actually consumes.
    features: Optional[dict] = None
    raw_candles: Optional[list] = None
    raw_candles_n = 0
    try:
        features = _build_features(candles)
    except Exception:
        features = None
    try:
        raw_candles = _build_raw_candles(candles, 60)
        raw_candles_n = len(raw_candles)
    except Exception:
        raw_candles = None
        raw_candles_n = 0

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
        # ---- recording-only fields below (consumed by the audit ledger only) ----
        "features": features,
        "raw_candles": raw_candles,
        "raw_candles_n": raw_candles_n,
        "candle_interval": "1m",
    }
