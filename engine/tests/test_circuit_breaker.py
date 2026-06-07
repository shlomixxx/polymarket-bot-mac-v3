import circuit_breaker as cb


_BASE = dict(streak=0, multiplier=1.0, cap=10.0, equity=10000.0, baseline=10000.0)


def test_disabled_never_halts():
    # master switch off -> never trips, regardless of conditions
    assert cb.should_halt(enabled=False, max_consecutive_losses=1, halt_at_cap=True,
                          equity_floor_pct=99.0, **{**_BASE, "streak": 50}) is None


def test_all_thresholds_at_sentinel_never_halts():
    # enabled but every threshold left at its disabled sentinel -> never trips
    assert cb.should_halt(enabled=True, max_consecutive_losses=0, halt_at_cap=False,
                          equity_floor_pct=0.0, **{**_BASE, "streak": 50, "multiplier": 99.0,
                                                   "equity": 1.0}) is None


def test_consecutive_losses_trips_at_threshold():
    assert cb.should_halt(enabled=True, max_consecutive_losses=5,
                          **{**_BASE, "streak": 4}) is None
    r = cb.should_halt(enabled=True, max_consecutive_losses=5, **{**_BASE, "streak": 5})
    assert r and "consecutive losses" in r


def test_halt_at_cap():
    assert cb.should_halt(enabled=True, halt_at_cap=True, **{**_BASE, "multiplier": 9.9}) is None
    r = cb.should_halt(enabled=True, halt_at_cap=True, **{**_BASE, "multiplier": 10.0})
    assert r and "cap" in r
    # cap of 1.0 (no real escalation) never trips
    assert cb.should_halt(enabled=True, halt_at_cap=True,
                          **{**_BASE, "cap": 1.0, "multiplier": 1.0}) is None


def test_equity_floor():
    # floor = 50% of baseline 10000 = 5000
    assert cb.should_halt(enabled=True, equity_floor_pct=50.0, **{**_BASE, "equity": 5001.0}) is None
    r = cb.should_halt(enabled=True, equity_floor_pct=50.0, **{**_BASE, "equity": 4999.0})
    assert r and "below floor" in r


def test_missing_equity_or_baseline_does_not_trip_floor():
    assert cb.should_halt(enabled=True, equity_floor_pct=50.0, **{**_BASE, "equity": None}) is None
    assert cb.should_halt(enabled=True, equity_floor_pct=50.0, **{**_BASE, "baseline": 0}) is None


def test_never_raises_on_garbage():
    assert cb.should_halt(enabled=True, max_consecutive_losses="x", streak=None,
                          multiplier=None, cap=None, equity="y", baseline=None,
                          halt_at_cap=True, equity_floor_pct="z") is None
