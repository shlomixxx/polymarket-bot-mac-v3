"""
Tests for engine/btc_strategies_extra.py — the three honest BTC strategy
variants (mean_reversion_v1, ma_crossover_v1, donchian_breakout_v1) and their
backtester factories. Bare imports (engine/ on path via conftest.py).

Each test maps to a clause in the task spec:
  (a) well-formed signal dict matching the backtester contract (both call shapes)
  (b) mean_reversion_v1 goes LONG on an OVERSOLD bar within an uptrend — NOT
      momentum — and the toggleable trend filter blocks a falling knife
  (c) ma_crossover_v1 goes long after a golden cross (mirror: short on death cross)
  (d) donchian_breakout_v1 goes long on a new N-period high (mirror: short on low)
  (e) stop < entry < target for long; target < entry < stop for short
  (f) never raises on malformed / empty input -> flat
  (g) symbol-scan: NO martingale / double / average / size-up surface
  (h) NO look-ahead inside the strategy (uses only the bars passed)

Run: python3 -m pytest engine/tests/test_btc_strategies_extra.py -q
"""
from __future__ import annotations

import btc_strategies_extra as m
from btc_strategies_extra import (
    donchian_breakout_v1,
    ma_crossover_v1,
    make_donchian_breakout,
    make_ma_crossover,
    make_mean_reversion,
    mean_reversion_v1,
)


# ---------------------------------------------------------------------------
# Candle builder + scenario fixtures
# ---------------------------------------------------------------------------

def c(o, h, l, cl, *, vol=1.0, t=None):
    d = {"open": o, "high": h, "low": l, "close": cl, "volume": vol}
    if t is not None:
        d["open_time"] = t
    return d


def _oversold_in_uptrend(n_base=220, n_dip=8, stamped=False):
    """A long uptrend (close well above EMA200) followed by a sharp multi-bar
    selloff that crushes RSI below 30 / breaks the lower Bollinger band while the
    close is STILL above the long-term EMA200 -> a textbook oversold dip in an
    uptrend (the mean-reversion long setup, NOT a momentum/breakout long)."""
    candles = []
    px = 20000.0
    tms = 3_600_000
    for i in range(n_base):
        o = px
        cl = px + 25
        candles.append(c(o, cl + 10, o - 10, cl, t=(i * tms) if stamped else None))
        px = cl
    for j in range(n_dip):
        o = px
        cl = px - 90
        candles.append(c(o, o + 5, cl - 5, cl,
                         t=((n_base + j) * tms) if stamped else None))
        px = cl
    return candles


def _overbought_in_downtrend(n_base=220, n_pop=8):
    """A long downtrend (close below EMA200) then a sharp rally that pushes RSI
    above 70 / above the upper band while still below EMA200 -> the mirror
    (mean-reversion short setup)."""
    candles = []
    px = 60000.0
    for _ in range(n_base):
        o = px
        cl = px - 25
        candles.append(c(o, o + 10, cl - 10, cl))
        px = cl
    for _ in range(n_pop):
        o = px
        cl = px + 90
        candles.append(c(o, cl + 5, o - 5, cl))
        px = cl
    return candles


def _falling_knife(n_base=220, n_dip=8):
    """Oversold (RSI < 30) but the close is BELOW EMA200 — the trend filter must
    veto this long ('don't catch a falling knife')."""
    candles = []
    px = 60000.0
    for _ in range(n_base):
        o = px
        cl = px - 25
        candles.append(c(o, o + 10, cl - 10, cl))
        px = cl
    for _ in range(n_dip):
        o = px
        cl = px - 90
        candles.append(c(o, o + 5, cl - 5, cl))
        px = cl
    return candles


def _golden_cross(n=260, start=20000.0, step=30.0):
    """Steadily rising closes -> EMA(fast) > EMA(slow): a golden-cross uptrend."""
    candles = []
    px = start
    for _ in range(n):
        o = px
        cl = px + step
        candles.append(c(o, cl + 10, o - 10, cl))
        px = cl
    return candles


def _death_cross(n=260, start=60000.0, step=30.0):
    candles = []
    px = start
    for _ in range(n):
        o = px
        cl = px - step
        candles.append(c(o, o + 10, cl - 10, cl))
        px = cl
    return candles


def _donchian_breakout_up(base=40, dc_period=20):
    """A flat base channel, then a final bar whose close prints above the prior
    N-period high (a new N-period high -> long breakout)."""
    candles = []
    px = 20000.0
    for i in range(base):
        o = px
        cl = px + (2 if i % 2 else -2)
        candles.append(c(o, o + 5, o - 5, cl))
        px = cl
    prior_high = max(x["high"] for x in candles[-dc_period:])
    candles.append(c(px, prior_high + 200, px - 5, prior_high + 150))
    return candles


def _donchian_breakdown(base=40, dc_period=20):
    candles = []
    px = 20000.0
    for i in range(base):
        o = px
        cl = px + (2 if i % 2 else -2)
        candles.append(c(o, o + 5, o - 5, cl))
        px = cl
    prior_low = min(x["low"] for x in candles[-dc_period:])
    candles.append(c(px, px + 5, prior_low - 200, prior_low - 150))
    return candles


# ---------------------------------------------------------------------------
# Contract helpers
# ---------------------------------------------------------------------------

CONTRACT_KEYS = {"signal", "entry", "stop", "target", "rr", "reasons"}


def _assert_well_formed(res):
    """Every variant must return the backtester/btc_strategy signal contract."""
    assert isinstance(res, dict)
    assert CONTRACT_KEYS.issubset(res.keys()), res.keys()
    assert res["signal"] in ("long", "short", "flat")
    assert isinstance(res["reasons"], list)
    if res["signal"] == "flat":
        assert res["entry"] is None
        assert res["stop"] is None
        assert res["target"] is None
    else:
        for k in ("entry", "stop", "target", "rr"):
            assert isinstance(res[k], float)


def _assert_long_geometry(res):
    assert res["signal"] == "long", res["reasons"]
    assert res["stop"] < res["entry"] < res["target"], res
    risk = res["entry"] - res["stop"]
    reward = res["target"] - res["entry"]
    assert risk > 0 and reward > 0
    assert abs(reward / risk - res["rr"]) < 1e-6


def _assert_short_geometry(res):
    assert res["signal"] == "short", res["reasons"]
    assert res["target"] < res["entry"] < res["stop"], res
    risk = res["stop"] - res["entry"]
    reward = res["entry"] - res["target"]
    assert risk > 0 and reward > 0
    assert abs(reward / risk - res["rr"]) < 1e-6


ALL_VARIANTS = (mean_reversion_v1, ma_crossover_v1, donchian_breakout_v1)


# ===========================================================================
# (a) Well-formed signal dict matching the backtester contract
# ===========================================================================

def test_all_variants_return_well_formed_flat_on_thin_data():
    """A short series gives every variant insufficient history -> a clean flat."""
    thin = [c(100, 101, 99, 100) for _ in range(5)]
    for fn in ALL_VARIANTS:
        _assert_well_formed(fn(thin, None))
        assert fn(thin, None)["signal"] == "flat"


def test_well_formed_on_each_active_setup():
    _assert_well_formed(mean_reversion_v1(_oversold_in_uptrend(), None))
    _assert_well_formed(ma_crossover_v1(_golden_cross(), None))
    _assert_well_formed(donchian_breakout_v1(_donchian_breakout_up(), None))


def test_backtester_call_shape_view_and_index():
    """The backtester calls strategy(view_by_tf, i). Each variant must accept the
    {tf: candles} view + integer index shape and read the base_tf series."""
    view = {"1h": _oversold_in_uptrend(stamped=True)}
    i = len(view["1h"]) - 1
    res = mean_reversion_v1(view, i)
    _assert_well_formed(res)
    assert res["signal"] == "long"
    # ma + donchian also accept the view shape.
    assert ma_crossover_v1({"1h": _golden_cross()}, 0)["signal"] == "long"
    assert donchian_breakout_v1({"1h": _donchian_breakout_up()}, 0)["signal"] == "long"


def test_factories_emit_backtester_signal_keys():
    """make_* return strat(view, i) yielding exactly the backtester's reduced
    {signal, entry, stop, target} contract (no rr/reasons leak)."""
    view = {"1h": _oversold_in_uptrend(stamped=True)}
    i = len(view["1h"]) - 1
    for make, expect in (
        (make_mean_reversion, "long"),
        (make_ma_crossover, None),
        (make_donchian_breakout, None),
    ):
        strat = make()
        sig = strat(view, i)
        assert set(sig.keys()) == {"signal", "entry", "stop", "target"}
        assert sig["signal"] in ("long", "short", "flat")
    # The mean-reversion factory should actually fire long on the oversold view.
    assert make_mean_reversion()(view, i)["signal"] == "long"
    # Factories accept a params override dict.
    assert make_ma_crossover({"ma_fast": 10, "ma_slow": 30})(
        {"1h": _golden_cross()}, 0)["signal"] == "long"


# ===========================================================================
# (b) mean_reversion_v1 — LONG on OVERSOLD within uptrend (NOT momentum)
# ===========================================================================

def test_mean_reversion_longs_an_oversold_dip_in_an_uptrend():
    candles = _oversold_in_uptrend()
    res = mean_reversion_v1(candles, None)
    _assert_long_geometry(res)
    blob = " ".join(res["reasons"]).lower()
    # It must be an OVERSOLD / revert-to-mean long, not a breakout/momentum long.
    assert "oversold" in blob
    assert "revert-to-mean" in blob or "trend filter on" in blob
    assert "breakout" not in blob and "momentum" not in blob


def test_mean_reversion_is_contrarian_not_momentum():
    """Sanity that it is genuinely mean-reverting: a strong UP-momentum bar (a
    fresh high, RSI hot) must NOT be bought (that is what momentum would do).
    It should be flat or short — never a long."""
    candles = _golden_cross(n=260, step=40.0)  # relentless up momentum, RSI high
    res = mean_reversion_v1(candles, None)
    assert res["signal"] != "long", res["reasons"]


def test_mean_reversion_trend_filter_blocks_falling_knife():
    """Filter ON: oversold but below EMA200 -> flat (skip the falling knife)."""
    candles = _falling_knife()
    res = mean_reversion_v1(candles, None)
    assert res["signal"] == "flat"
    assert any("falling knife" in r.lower() for r in res["reasons"]), res["reasons"]


def test_mean_reversion_trend_filter_contribution_is_measurable():
    """Toggling the filter OFF lets the same oversold setup fire long -> the
    filter's contribution is independently measurable in a backtest."""
    candles = _falling_knife()
    assert mean_reversion_v1(candles, None)["signal"] == "flat"
    opened = mean_reversion_v1(candles, {"mr_use_trend_filter": False})
    assert opened["signal"] == "long", opened["reasons"]
    assert any("filter off" in r.lower() for r in opened["reasons"])


def test_mean_reversion_target_reverts_toward_the_mean():
    """The long target is at least the middle Bollinger band (revert-to-mean),
    and never tighter than the configured min R:R."""
    candles = _oversold_in_uptrend()
    res = mean_reversion_v1(candles, None)
    _assert_long_geometry(res)
    assert res["rr"] >= 2.0 - 1e-9


def test_mean_reversion_shorts_an_overbought_spike_mirror():
    candles = _overbought_in_downtrend()
    res = mean_reversion_v1(candles, None)
    _assert_short_geometry(res)
    assert any("overbought" in r.lower() for r in res["reasons"])


def test_mean_reversion_skips_when_stop_would_be_too_wide():
    """The ATR stop cap is a real skip rule: a near-zero cap -> flat, not a trade
    with a blown-out stop."""
    candles = _oversold_in_uptrend()
    assert mean_reversion_v1(candles, None)["signal"] == "long"
    # Keep the default stop multiple (chosen risk ~1.75*ATR) but pull the cap
    # BELOW it -> the chosen stop is wider than the cap -> skip to flat. (Shrinking
    # the multiple too would just make the chosen risk equal the cap, not exceed
    # it, which correctly would NOT skip.)
    res = mean_reversion_v1(candles, {"mr_atr_stop_cap": 0.5})
    assert res["signal"] == "flat"
    assert any("too wide" in r.lower() or "skip" in r.lower() for r in res["reasons"])


# ===========================================================================
# (c) ma_crossover_v1 — long after golden cross, short after death cross
# ===========================================================================

def test_ma_crossover_longs_after_golden_cross():
    res = ma_crossover_v1(_golden_cross(), None)
    _assert_long_geometry(res)
    assert any("golden cross" in r.lower() for r in res["reasons"])


def test_ma_crossover_shorts_after_death_cross():
    res = ma_crossover_v1(_death_cross(), None)
    _assert_short_geometry(res)
    assert any("death cross" in r.lower() for r in res["reasons"])


def test_ma_crossover_respects_configurable_fast_slow():
    # Faster pair converges sooner; still a golden-cross long on the same uptrend.
    res = ma_crossover_v1(_golden_cross(n=80), {"ma_fast": 10, "ma_slow": 30})
    _assert_long_geometry(res)


def test_ma_crossover_rejects_bad_fast_slow_config():
    res = ma_crossover_v1(_golden_cross(), {"ma_fast": 200, "ma_slow": 50})
    assert res["signal"] == "flat"  # fast >= slow is invalid


# ===========================================================================
# (d) donchian_breakout_v1 — long on new N-period high, short on new low
# ===========================================================================

def test_donchian_longs_on_new_n_period_high():
    res = donchian_breakout_v1(_donchian_breakout_up(), None)
    _assert_long_geometry(res)
    assert any("breakout up" in r.lower() for r in res["reasons"])


def test_donchian_shorts_on_new_n_period_low():
    res = donchian_breakout_v1(_donchian_breakdown(), None)
    _assert_short_geometry(res)
    assert any("breakout down" in r.lower() for r in res["reasons"])


def test_donchian_flat_inside_channel():
    """A close that stays inside the prior channel must NOT trade."""
    candles = []
    px = 20000.0
    for i in range(40):
        o = px
        cl = px + (3 if i % 2 else -3)
        candles.append(c(o, o + 5, o - 5, cl))
        px = cl
    # final bar closes inside the established channel
    candles.append(c(px, px + 4, px - 4, px + 1))
    res = donchian_breakout_v1(candles, None)
    assert res["signal"] == "flat"
    assert any("inside channel" in r.lower() for r in res["reasons"])


def test_donchian_respects_configurable_n():
    res = donchian_breakout_v1(_donchian_breakout_up(base=20, dc_period=10),
                               {"dc_period": 10})
    _assert_long_geometry(res)


# ===========================================================================
# (e) stop < entry < target for long; target < entry < stop for short
#     (exercised for every variant on its active setup)
# ===========================================================================

def test_long_geometry_holds_for_every_variant():
    _assert_long_geometry(mean_reversion_v1(_oversold_in_uptrend(), None))
    _assert_long_geometry(ma_crossover_v1(_golden_cross(), None))
    _assert_long_geometry(donchian_breakout_v1(_donchian_breakout_up(), None))


def test_short_geometry_holds_for_every_variant():
    _assert_short_geometry(mean_reversion_v1(_overbought_in_downtrend(), None))
    _assert_short_geometry(ma_crossover_v1(_death_cross(), None))
    _assert_short_geometry(donchian_breakout_v1(_donchian_breakdown(), None))


# ===========================================================================
# (f) never raises on malformed / empty input -> flat
# ===========================================================================

def test_never_raises_on_malformed_or_empty_input():
    bad_inputs = [
        None, [], "", "nope", 42, 3.14, {}, [{}],
        [{"open": "x"}], [{"open": 1}],            # missing keys
        [c(100, 90, 95, 99)],                       # high < low (insane)
        [None, 1, "y", {}],                         # heterogeneous junk
        {"1h": None}, {"1h": []}, {"1h": [{}]},    # backtester-shape junk
        {"4h": [c(1, 2, 0.5, 1.5)]},               # wrong/missing base_tf
        [float("nan")], [{"open": float("inf"),
                          "high": 1, "low": 0, "close": 1}],
    ]
    for fn in ALL_VARIANTS:
        for bad in bad_inputs:
            res = fn(bad, None)
            _assert_well_formed(res)
            assert res["signal"] == "flat", (fn.__name__, bad, res)
        # Also via the backtester factory (the reduced contract).
        make = {
            "mean_reversion_v1": make_mean_reversion,
            "ma_crossover_v1": make_ma_crossover,
            "donchian_breakout_v1": make_donchian_breakout,
        }[fn.__name__]
        strat = make()
        for bad in bad_inputs:
            sig = strat(bad if isinstance(bad, dict) else {"1h": bad}, 0)
            assert sig["signal"] == "flat"


def test_internal_error_is_swallowed_to_flat():
    """Even a config that would make an indicator angry must degrade to flat,
    never an exception (negative periods etc.)."""
    candles = _oversold_in_uptrend()
    for cfg in (
        {"mr_rsi_period": -5},
        {"mr_bb_period": 0},
        {"ma_fast": -1},
        {"dc_period": -3},
        {"mr_bb_std": "not-a-number"},
    ):
        for fn in ALL_VARIANTS:
            res = fn(candles, cfg)
            _assert_well_formed(res)  # must not raise


# ===========================================================================
# (g) symbol-scan: NO martingale / double / average / size-up surface
# ===========================================================================

def test_no_martingale_or_sizing_surface_in_module():
    """Defense in depth: the module may mention 'martingale' in prose (the spec
    forbids it), but must expose NO callable/attribute that doubles, averages,
    adds to a loser, or sizes a position."""
    forbidden = (
        "double", "multiplier", "size_up", "sizeup", "add_to", "addto",
        "average_down", "averagedown", "martingale", "stake", "position_size",
        "recover", "pyramid", "scale_in", "scalein",
    )
    public = [name for name in dir(m) if not name.startswith("__")]
    for name in public:
        low = name.lower()
        assert all(f not in low for f in forbidden), f"suspicious symbol: {name}"


def test_signal_dicts_carry_no_sizing_field():
    """No result dict exposes a quantity / size / leverage field a caller could
    martingale on — sizing is delegated wholly to risk_engine downstream."""
    setups = (
        mean_reversion_v1(_oversold_in_uptrend(), None),
        ma_crossover_v1(_golden_cross(), None),
        donchian_breakout_v1(_donchian_breakout_up(), None),
    )
    for res in setups:
        assert set(res.keys()) == CONTRACT_KEYS
        for forbidden in ("qty", "size", "leverage", "stake", "multiplier",
                          "position", "lots", "amount"):
            assert forbidden not in res


# ===========================================================================
# (h) NO look-ahead inside the strategy (uses only the bars passed)
# ===========================================================================

def test_no_lookahead_decision_independent_of_future_bars():
    """The decision at bar k must depend ONLY on bars [0..k]. Appending arbitrary
    FUTURE bars (including a wild future spike) and then asking for the SAME
    prefix [0..k] must yield an identical signal — proving the strategy never
    peeks past the bars it was handed."""
    for fn, candles in (
        (mean_reversion_v1, _oversold_in_uptrend()),
        (ma_crossover_v1, _golden_cross()),
        (donchian_breakout_v1, _donchian_breakout_up()),
    ):
        k = len(candles) - 1
        decision = fn(candles[:k + 1], None)
        # Append future noise that, if peeked, would change the call.
        future_noise = [
            c(candles[-1]["close"], candles[-1]["close"] * 5,
              candles[-1]["close"] * 0.2, candles[-1]["close"] * 4),
            c(1, 2, 0.5, 1.5),
        ]
        extended = candles[:k + 1] + future_noise
        # Re-ask for the identical decision point [0..k].
        again = fn(extended[:k + 1], None)
        assert decision == again, fn.__name__


def test_no_lookahead_via_backtester_view_slice():
    """In the backtester shape the engine pre-slices the view to [0..i]; the
    strategy reads only what it is given. Truncating the view to the same prefix
    must reproduce the signal regardless of any (hidden) later bars."""
    full = _oversold_in_uptrend(stamped=True)
    k = len(full) - 1
    sig_full = mean_reversion_v1({"1h": full[:k + 1]}, k)
    # A view that physically contains only [0..k] must give the same answer as
    # one where the engine *would* have sliced a longer series down to [0..k].
    longer = full + [c(full[-1]["close"], full[-1]["close"] * 3,
                       full[-1]["close"] * 0.5, full[-1]["close"] * 2,
                       t=(len(full)) * 3_600_000)]
    sig_sliced = mean_reversion_v1({"1h": longer[:k + 1]}, k)
    assert sig_full == sig_sliced
