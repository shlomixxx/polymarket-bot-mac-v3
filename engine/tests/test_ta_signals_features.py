"""
Tests for the recording-only ML-trading feature enrichment in ta_signals.

These features are DATA-ONLY: they must never change the trading score /
signals / existing top-level fields. The tests verify:
  1. The pure indicator helpers against hand-computed values.
  2. compute_ta_signals's contract is preserved (top-level fields + score
     identical with and without the new code path) and that `features` +
     `raw_candles` are populated.
"""
from __future__ import annotations

import math

import pytest

import ta_signals
from ta_signals import (
    _build_features,
    _build_raw_candles,
    _ema,
    compute_bollinger,
    compute_macd,
    compute_obv,
    compute_rsi,
    compute_stochastic,
    log_return,
    obv_slope,
    realized_vol,
    volume_zscore,
)


# --------------------------------------------------------------------------
# Helpers / fixtures
# --------------------------------------------------------------------------

def _mk_candles(closes, volumes=None, highs=None, lows=None, opens=None):
    """Build candle dicts from parallel lists; defaults derive from closes."""
    n = len(closes)
    volumes = volumes if volumes is not None else [1.0] * n
    highs = highs if highs is not None else [c + 1.0 for c in closes]
    lows = lows if lows is not None else [c - 1.0 for c in closes]
    opens = opens if opens is not None else list(closes)
    out = []
    for i in range(n):
        out.append({
            "open_time": i,
            "open": opens[i],
            "high": highs[i],
            "low": lows[i],
            "close": closes[i],
            "volume": volumes[i],
        })
    return out


# --------------------------------------------------------------------------
# MACD
# --------------------------------------------------------------------------

def test_macd_none_when_insufficient_data():
    # Needs slow+signal = 26+9 = 35 closes.
    closes = [100.0 + i for i in range(34)]
    macd, sig, hist = compute_macd(closes)
    assert macd is None and sig is None and hist is None


def test_macd_hist_equals_macd_minus_signal():
    closes = [100.0 + math.sin(i / 3.0) * 5 for i in range(60)]
    macd, sig, hist = compute_macd(closes)
    assert macd is not None and sig is not None and hist is not None
    assert hist == pytest.approx(macd - sig, abs=1e-9)


def test_macd_matches_manual_ema_difference():
    # On a strictly linear ramp, MACD = EMA12 - EMA26 on aligned bars.
    closes = [100.0 + i * 0.5 for i in range(60)]
    macd, sig, hist = compute_macd(closes)
    ema_fast = _ema(closes, 12)
    ema_slow = _ema(closes, 26)
    n = min(len(ema_fast), len(ema_slow))
    expected_macd = ema_fast[-n:][-1] - ema_slow[-n:][-1]
    assert macd == pytest.approx(expected_macd, abs=1e-9)


# --------------------------------------------------------------------------
# RSI multi-period (reuses compute_rsi)
# --------------------------------------------------------------------------

def test_rsi_all_gains_is_100():
    closes = [100.0 + i for i in range(40)]
    assert compute_rsi(closes, 7) == pytest.approx(100.0)
    assert compute_rsi(closes, 30) == pytest.approx(100.0)


def test_rsi30_none_when_short():
    closes = [100.0 + i for i in range(20)]  # < 31
    assert compute_rsi(closes, 30) is None


# --------------------------------------------------------------------------
# Stochastic
# --------------------------------------------------------------------------

def test_stochastic_k_at_top_of_range():
    # Last close is the highest high over the window -> %K = 100.
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    highs = [c for c in closes]
    lows = [c - 10 for c in closes]
    candles = _mk_candles(closes, highs=highs, lows=lows)
    k, d = compute_stochastic(candles, 14, 3)
    # highest_high over window = 23 (last high), lowest_low = 10-10=0... but
    # window highs are the closes themselves -> hh=23; lows min = 0; close=23.
    assert k == pytest.approx(100.0)


def test_stochastic_manual_value():
    # Window of 14; close=15, lowest_low=10, highest_high=20 -> %K=50.
    closes = [12] * 13 + [15]
    highs = [20] + [13] * 12 + [16]
    lows = [10] + [11] * 12 + [14]
    candles = _mk_candles(closes, highs=highs, lows=lows)
    k, d = compute_stochastic(candles, 14, 3)
    # hh=20, ll=10, close=15 -> (15-10)/(20-10)*100 = 50
    assert k == pytest.approx(50.0)


def test_stochastic_flat_range_returns_50():
    closes = [100.0] * 14
    candles = _mk_candles(closes, highs=[100.0] * 14, lows=[100.0] * 14)
    k, d = compute_stochastic(candles, 14, 3)
    assert k == pytest.approx(50.0)


def test_stochastic_d_none_when_too_short():
    closes = [10 + i for i in range(14)]  # exactly period, no room for %D smoothing
    candles = _mk_candles(closes)
    k, d = compute_stochastic(candles, 14, 3)
    assert k is not None
    assert d is None


# --------------------------------------------------------------------------
# Bollinger
# --------------------------------------------------------------------------

def test_bollinger_constant_series():
    closes = [100.0] * 20
    pct_b, bw = compute_bollinger(closes, 20, 2.0)
    # std=0 -> band=0 -> pct_b defaults to 0.5, bandwidth=0
    assert pct_b == pytest.approx(0.5)
    assert bw == pytest.approx(0.0)


def test_bollinger_manual():
    # 20 values: nineteen 100s and one 120 at the end.
    closes = [100.0] * 19 + [120.0]
    pct_b, bw = compute_bollinger(closes, 20, 2.0)
    mid = sum(closes) / 20
    var = sum((c - mid) ** 2 for c in closes) / 20
    std = math.sqrt(var)
    upper = mid + 2 * std
    lower = mid - 2 * std
    expected_pct_b = (120.0 - lower) / (upper - lower)
    expected_bw = (upper - lower) / mid
    assert pct_b == pytest.approx(expected_pct_b)
    assert bw == pytest.approx(expected_bw)


def test_bollinger_none_when_short():
    assert compute_bollinger([1.0] * 19, 20) == (None, None)


# --------------------------------------------------------------------------
# Log returns
# --------------------------------------------------------------------------

def test_log_return_manual():
    closes = [100.0, 101.0, 102.0, 110.0]
    assert log_return(closes, 1) == pytest.approx(math.log(110.0 / 102.0))
    assert log_return(closes, 3) == pytest.approx(math.log(110.0 / 100.0))


def test_log_return_none_when_short():
    assert log_return([100.0], 1) is None
    assert log_return([100.0, 101.0], 5) is None


# --------------------------------------------------------------------------
# Realized volatility
# --------------------------------------------------------------------------

def test_realized_vol_manual():
    closes = [100.0, 110.0, 99.0]
    # two returns: ln(110/100), ln(99/110)
    r1 = math.log(110.0 / 100.0)
    r2 = math.log(99.0 / 110.0)
    expected = math.sqrt(r1 * r1 + r2 * r2)
    assert realized_vol(closes, 2) == pytest.approx(expected)


def test_realized_vol_none_when_short():
    assert realized_vol([100.0, 101.0], 5) is None


# --------------------------------------------------------------------------
# OBV / slope / volume z-score
# --------------------------------------------------------------------------

def test_obv_manual():
    closes = [100.0, 101.0, 100.0, 102.0]
    volumes = [10.0, 5.0, 3.0, 7.0]
    # +5 (up), -3 (down), +7 (up) = 9
    assert compute_obv(closes, volumes) == pytest.approx(9.0)


def test_obv_none_on_mismatch():
    assert compute_obv([1.0, 2.0], [1.0]) is None
    assert compute_obv([1.0], [1.0]) is None


def test_obv_slope_direction():
    closes = [100.0 + i for i in range(20)]  # strictly rising
    volumes = [1.0] * 20
    slope = obv_slope(closes, volumes, 10)
    assert slope is not None and slope > 0


def test_volume_zscore_manual():
    # 20 reference bars all = 10, then a latest of 30 with std 0 -> 0.0
    volumes = [10.0] * 20 + [30.0]
    assert volume_zscore(volumes, 20) == pytest.approx(0.0)


def test_volume_zscore_nonzero():
    volumes = [10.0, 12.0, 8.0, 11.0, 9.0] * 4 + [50.0]  # 20 ref + latest
    z = volume_zscore(volumes, 20)
    assert z is not None and z > 0


def test_volume_zscore_none_when_short():
    assert volume_zscore([1.0] * 10, 20) is None


# --------------------------------------------------------------------------
# _build_features guards on short data
# --------------------------------------------------------------------------

def test_build_features_short_series_no_raise_all_none_heavy():
    candles = _mk_candles([100.0 + i for i in range(5)])
    feats = _build_features(candles)
    # Heavy/long indicators must be None, not raise.
    for key in ("macd_pct", "rsi30", "stoch_k", "ema50", "ema200", "rv_30"):
        assert feats[key] is None
    # Short-window features can still compute.
    assert feats["ret_1m"] is not None


def test_build_features_full_series_populated():
    closes = [100.0 + math.sin(i / 5.0) * 10 for i in range(250)]
    volumes = [100.0 + (i % 7) for i in range(250)]
    candles = _mk_candles(closes, volumes=volumes)
    feats = _build_features(candles)
    expected_keys = {
        "macd_pct", "macd_signal_pct", "macd_hist_pct",
        "rsi7", "rsi30",
        "stoch_k", "stoch_d", "stoch_k_30",
        "bb_pct_b", "bb_bandwidth",
        "ret_1m", "ret_2m", "ret_3m", "ret_5m", "ret_10m", "ret_15m",
        "rv_5", "rv_15", "rv_30",
        "ema50", "ema200", "ema9_21_ratio", "price_vs_ema21_pct",
        "volume", "volume_z", "obv", "obv_slope",
        "atr_pct",
    }
    assert expected_keys.issubset(set(feats.keys()))
    for k in expected_keys:
        assert feats[k] is not None, f"{k} unexpectedly None on full series"


# --------------------------------------------------------------------------
# raw candle capture
# --------------------------------------------------------------------------

def test_build_raw_candles_caps_at_60():
    candles = _mk_candles([100.0 + i for i in range(250)],
                          volumes=[float(i) for i in range(250)])
    raw = _build_raw_candles(candles, 60)
    assert len(raw) == 60
    # Each row is [open, high, low, close, volume].
    assert len(raw[0]) == 5
    # Last row corresponds to the last candle.
    last = candles[-1]
    assert raw[-1][3] == pytest.approx(last["close"])
    assert raw[-1][4] == pytest.approx(last["volume"])


def test_build_raw_candles_shorter_than_60():
    candles = _mk_candles([100.0 + i for i in range(10)])
    raw = _build_raw_candles(candles, 60)
    assert len(raw) == 10


# --------------------------------------------------------------------------
# Integration: compute_ta_signals contract preserved + new fields populated
# --------------------------------------------------------------------------

def _synthetic_klines(n=250):
    closes = [100.0 + math.sin(i / 4.0) * 8 + i * 0.01 for i in range(n)]
    return _mk_candles(
        closes,
        volumes=[1000.0 + (i % 11) * 10 for i in range(n)],
    )


@pytest.mark.asyncio
async def test_compute_ta_signals_contract_and_new_fields(monkeypatch):
    synthetic = _synthetic_klines(250)

    async def fake_fetch(interval="1m", limit=250):
        return synthetic

    monkeypatch.setattr(ta_signals, "fetch_btc_klines", fake_fetch)
    result = await ta_signals.compute_ta_signals()

    # --- existing contract preserved ---
    for key in (
        "available", "current_price", "rsi", "ema9", "ema21", "atr",
        "momentum_3m_pct", "momentum_5m_pct", "score", "max_score",
        "signals", "ts",
    ):
        assert key in result
    assert result["available"] is True
    assert result["max_score"] == 4
    assert isinstance(result["score"], int)
    assert isinstance(result["signals"], list)

    # --- new recording-only fields populated ---
    assert isinstance(result["features"], dict)
    assert result["features"]["macd_pct"] is not None
    assert result["features"]["rsi7"] is not None
    assert isinstance(result["raw_candles"], list)
    assert result["raw_candles_n"] == 60
    assert len(result["raw_candles"]) == 60
    assert result["candle_interval"] == "1m"


@pytest.mark.asyncio
async def test_compute_ta_signals_score_unchanged_by_recording(monkeypatch):
    """The score/signals must be identical to what the *legacy* logic produces
    from the same closes — proving the recording features don't leak in."""
    synthetic = _synthetic_klines(250)

    async def fake_fetch(interval="1m", limit=250):
        return synthetic

    monkeypatch.setattr(ta_signals, "fetch_btc_klines", fake_fetch)
    result = await ta_signals.compute_ta_signals()

    # Recompute the score independently using only the legacy inputs.
    closes = [c["close"] for c in synthetic]
    cur = closes[-1]
    rsi = compute_rsi(closes)
    ema9 = _ema(closes, 9)[-1]
    ema21 = _ema(closes, 21)[-1]
    mom3 = (cur - closes[-4]) / closes[-4] * 100
    mom5 = (cur - closes[-6]) / closes[-6] * 100
    expected_score = 0
    if rsi > 55:
        expected_score += 1
    elif rsi < 45:
        expected_score -= 1
    if ema9 - ema21 > 0:
        expected_score += 1
    else:
        expected_score -= 1
    if mom3 > 0.05:
        expected_score += 1
    elif mom3 < -0.05:
        expected_score -= 1
    if mom5 > 0.08:
        expected_score += 1
    elif mom5 < -0.08:
        expected_score -= 1

    assert result["score"] == expected_score


@pytest.mark.asyncio
async def test_compute_ta_signals_never_raises_on_feature_failure(monkeypatch):
    """If feature building blows up, the signal still returns with features=None."""
    synthetic = _synthetic_klines(250)

    async def fake_fetch(interval="1m", limit=250):
        return synthetic

    def boom(_candles):
        raise RuntimeError("feature explosion")

    monkeypatch.setattr(ta_signals, "fetch_btc_klines", fake_fetch)
    monkeypatch.setattr(ta_signals, "_build_features", boom)
    result = await ta_signals.compute_ta_signals()

    assert result["available"] is True
    assert result["features"] is None
    # core score still present
    assert "score" in result
