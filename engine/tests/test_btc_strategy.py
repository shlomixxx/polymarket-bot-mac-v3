"""
Tests for engine/btc_strategy.py — the trend_pullback_v1 signal engine and the
pure candlestick detectors. Bare imports (engine/ on path via conftest.py).

Run: python3 -m pytest engine/tests/test_btc_strategy.py -q
"""
from __future__ import annotations

import btc_strategy as S
from btc_strategy import (
    bearish_engulfing,
    bullish_engulfing,
    doji,
    evaluate_signal,
    evening_star,
    hammer,
    morning_star,
    pin_bar,
)


# ---------------------------------------------------------------------------
# Candle builder
# ---------------------------------------------------------------------------

def c(o, h, l, cl, *, vol=1.0, t=None):
    d = {"open": o, "high": h, "low": l, "close": cl, "volume": vol}
    if t is not None:
        d["open_time"] = t
    return d


# ---------------------------------------------------------------------------
# Candlestick detector tests
# ---------------------------------------------------------------------------

def test_bullish_engulfing_true():
    prev = c(100, 101, 95, 96)      # bearish, body 96..100
    cur = c(95, 106, 94, 105)       # bullish, body 95..105 engulfs 96..100
    assert bullish_engulfing([prev, cur]) is True


def test_bullish_engulfing_false_when_not_engulfing():
    prev = c(100, 101, 95, 96)
    cur = c(97, 99, 96, 98)         # bullish but small, doesn't engulf
    assert bullish_engulfing([prev, cur]) is False


def test_bullish_engulfing_false_when_prev_bullish():
    prev = c(95, 106, 94, 105)      # prev was bullish
    cur = c(95, 110, 94, 109)
    assert bullish_engulfing([prev, cur]) is False


def test_bearish_engulfing_true():
    prev = c(100, 106, 99, 105)     # bullish, body 100..105
    cur = c(106, 107, 98, 99)       # bearish, body 99..106 engulfs 100..105
    assert bearish_engulfing([prev, cur]) is True


def test_hammer_pin_bar_bullish():
    # small body near the top, long lower wick (rejection of lows)
    candle = c(100, 100.5, 94, 100.2)
    assert pin_bar([candle], "bullish") is True
    assert hammer([candle]) is True


def test_pin_bar_bearish_shooting_star():
    candle = c(100, 106, 99.8, 100.2)  # long upper wick
    assert pin_bar([candle], "bearish") is True


def test_pin_bar_false_on_balanced_candle():
    candle = c(100, 102, 98, 101)  # symmetric-ish, no dominant wick
    assert pin_bar([candle], "bullish") is False


def test_morning_star_true():
    c1 = c(100, 100.5, 92, 93)      # big bearish
    c2 = c(92.5, 93.5, 91.5, 92.8)  # small star
    c3 = c(93, 101, 92.5, 100)      # big bullish closing above midpoint of c1 (96.5)
    assert morning_star([c1, c2, c3]) is True


def test_evening_star_true():
    c1 = c(93, 101, 92.5, 100)      # big bullish
    c2 = c(100.2, 101, 99.8, 100.3)  # small star
    c3 = c(100, 100.5, 92, 93)      # big bearish closing below midpoint of c1 (96.5)
    assert evening_star([c1, c2, c3]) is True


def test_doji_true():
    assert doji([c(100, 102, 98, 100.05)]) is True


def test_doji_false_on_real_body():
    assert doji([c(100, 102, 98, 101.5)]) is False


def test_detectors_tolerate_malformed_input():
    bad_inputs = [None, [], [{}], [{"open": "x"}], "nope", [c(100, 90, 95, 99)]]
    for bad in bad_inputs:
        assert bullish_engulfing(bad) is False
        assert bearish_engulfing(bad) is False
        assert pin_bar(bad) is False
        assert morning_star(bad) is False
        assert evening_star(bad) is False
        assert doji(bad) is False


# ---------------------------------------------------------------------------
# Fixtures for evaluate_signal
# ---------------------------------------------------------------------------

def _uptrend_daily(n=260, start=20000.0, step=40.0):
    """Steadily rising daily closes -> price > EMA200 and EMA50 > EMA200."""
    candles = []
    px = start
    for _ in range(n):
        o = px
        cl = px + step
        candles.append(c(o, cl + 5, o - 5, cl))
        px = cl
    return candles


def _downtrend_daily(n=260, start=60000.0, step=40.0):
    candles = []
    px = start
    for _ in range(n):
        o = px
        cl = px - step
        candles.append(c(o, o + 5, cl - 5, cl))
        px = cl
    return candles


def _h4_pullback_long(liquid_hour=True):
    """4h frame: rising, then a pullback that drags RSI into the 40-50 band and
    brings price back to a prior swing low, ending on a bullish-engulfing trigger
    whose lower wick gives a structural stop comfortably within 2*ATR."""
    candles = []
    px = 30000.0
    # rising leg to build EMA21 below price and warm up RSI/ATR
    for _ in range(40):
        o = px
        cl = px + 60
        candles.append(c(o, cl + 20, o - 20, cl))
        px = cl
    # pullback leg: enough down candles to drag RSI into 40-50 and revisit a level
    for _ in range(12):
        o = px
        cl = px - 60
        candles.append(c(o, o + 15, cl - 15, cl))
        px = cl
    # trigger: bullish engulfing closing back near the swing low (tight wick stop)
    prev = c(px, px + 10, px - 40, px - 30)        # small bearish
    base = px - 30
    trig_open = base
    trig_close = base + 60                          # bullish, engulfs prev body
    trig_low = base - 30                            # wick -> structural stop
    trig_high = trig_close + 10
    # pick a ms epoch whose UTC hour (14:00) is in the liquid overlap window
    t = 1705329000000 if liquid_hour else None
    trig = c(trig_open, trig_high, trig_low, trig_close, t=t)
    candles.append(prev)
    candles.append(trig)
    return candles


def _h1_bullish_trigger(liquid=True):
    """1h entry frame ending on a clean bullish engulfing with a tight wick."""
    candles = []
    px = 30000.0
    # rising leg with enough per-bar range to give a sane ATR(14) on the 1h frame,
    # so the trigger candle's wick stop sits comfortably inside 2*ATR.
    for _ in range(30):
        o = px
        cl = px + 5
        candles.append(c(o, cl + 20, o - 20, cl))
        px = cl
    prev = c(px, px + 3, px - 15, px - 12)   # small bearish
    base = px - 12
    trig_open = base
    trig_close = base + 40                    # bullish engulfing
    trig_low = base - 10                      # tight wick -> small structural risk
    trig_high = trig_close + 3
    t = None
    if liquid:
        t = 1705329000000  # UTC hour 14
    trig = c(trig_open, trig_high, trig_low, trig_close, t=t)
    candles.append(prev)
    candles.append(trig)
    return candles


# ---------------------------------------------------------------------------
# evaluate_signal tests
# ---------------------------------------------------------------------------

def test_clean_uptrend_pullback_engulfing_goes_long():
    daily = _uptrend_daily()
    h4 = _h4_pullback_long()
    h1 = _h1_bullish_trigger()
    res = evaluate_signal(daily, h4, h1)
    assert res["signal"] == "long", res["reasons"]
    assert res["gates"]["trend"] is True
    assert res["gates"]["pullback"] is True
    assert res["gates"]["trigger"] is True
    assert res["gates"]["liquidity"] is True
    # stop < entry < target and rr >= 2
    assert res["stop"] < res["entry"] < res["target"]
    assert res["rr"] >= 2.0
    # target geometry honors the R:R
    risk = res["entry"] - res["stop"]
    reward = res["target"] - res["entry"]
    assert abs(reward / risk - res["rr"]) < 1e-6
    assert isinstance(res["reasons"], list) and res["reasons"]


def test_downtrend_blocks_long_via_trend_gate():
    daily = _downtrend_daily()
    h4 = _h4_pullback_long()
    h1 = _h1_bullish_trigger()
    res = evaluate_signal(daily, h4, h1)
    # In a confirmed downtrend, the bullish setup must NOT produce a long.
    assert res["signal"] != "long"


def test_stop_too_wide_returns_flat():
    daily = _uptrend_daily()
    h4 = _h4_pullback_long()
    h1 = _h1_bullish_trigger()
    # Force the SKIP rule: a tiny ATR cap makes the structural wick stop "too wide".
    cfg = {"atr_stop_mult": 0.01, "atr_stop_mult_tight": 0.01}
    res = evaluate_signal(daily, h4, h1, config=cfg)
    assert res["signal"] == "flat"
    assert any("too wide" in r or "SKIP" in r for r in res["reasons"]), res["reasons"]


def test_malformed_input_is_flat_never_raises():
    for bad in (None, [], "x", [{}], 42):
        res = evaluate_signal(bad, bad, bad)
        assert res["signal"] == "flat"
        assert res["entry"] is None
        assert isinstance(res["reasons"], list)


def test_disabling_trend_gate_is_measurable():
    """Each gate is a separate flag; disabling trend gate flips a blocked long."""
    daily = _downtrend_daily()
    h4 = _h4_pullback_long()
    h1 = _h1_bullish_trigger()
    blocked = evaluate_signal(daily, h4, h1)
    assert blocked["signal"] != "long"
    # With the trend gate off, the same bullish setup can now fire long.
    opened = evaluate_signal(daily, h4, h1, config={"use_trend_gate": False})
    assert opened["signal"] == "long", opened["reasons"]
    assert "trend gate disabled by config" in " ".join(opened["reasons"])


def test_no_martingale_surface_in_module():
    """Defense in depth: no position-sizing / averaging / martingale CODE surface.

    The module may *mention* martingale in prose (the spec forbids it), but it
    must expose no callable or attribute that doubles/sizes/averages a position.
    """
    forbidden = ("double", "multiplier", "size_up", "add_to", "average_down",
                 "martingale_step", "stake")
    public = [name for name in dir(S) if not name.startswith("__")]
    for name in public:
        low = name.lower()
        assert all(f not in low for f in forbidden), f"suspicious symbol: {name}"
    # And the result dict carries no sizing field a caller could martingale on.
    daily = _uptrend_daily()
    res = evaluate_signal(daily, _h4_pullback_long(), _h1_bullish_trigger())
    assert set(res.keys()) == {
        "signal", "entry", "stop", "target", "rr", "reasons", "gates"
    }
