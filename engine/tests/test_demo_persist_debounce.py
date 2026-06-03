"""PR-D: ה-persist (כתיבת 6MB) מושהה (debounced) ומתבצע מחוץ ל-event-loop (asyncio.to_thread),
כדי שלא יחנוק את הלולאה בכל poll — זה היה גורם ה-outage. גם ה-backfill מוגבל בתדירות."""
import asyncio
from pathlib import Path

import pytest

import demo_engine
from demo_engine import DemoEngine, DemoState


@pytest.mark.asyncio
async def test_persist_is_debounced_and_offloop(tmp_path: Path, monkeypatch):
    eng = DemoEngine(state_path=tmp_path / "s.json")
    writes = {"n": 0}
    monkeypatch.setattr(demo_engine, "atomic_write_json",
                        lambda *a, **k: writes.__setitem__("n", writes["n"] + 1))

    eng._mark_dirty()
    await eng._maybe_persist_async()
    await asyncio.sleep(0.2)  # fire-and-forget write completes
    assert writes["n"] == 1  # first persist fired

    # immediate second request — within PERSIST_INTERVAL_SEC → debounced, no extra 6MB write
    eng._mark_dirty()
    await eng._maybe_persist_async()
    await asyncio.sleep(0.2)
    assert writes["n"] == 1  # still 1 — debounced (the per-poll-save outage cannot recur)


@pytest.mark.asyncio
async def test_persist_single_flight(tmp_path: Path, monkeypatch):
    """כשכתיבה אחת עדיין רצה (איטית) — לא מתחילה כתיבה שנייה במקביל (single-flight)."""
    eng = DemoEngine(state_path=tmp_path / "s.json")
    eng._last_persist_ts = 0.0
    started = {"n": 0}

    def slow_write(*a, **k):
        started["n"] += 1
        import time as _t
        _t.sleep(0.5)  # simulate a slow 6MB fsync

    monkeypatch.setattr(demo_engine, "atomic_write_json", slow_write)
    eng._mark_dirty()
    await eng._maybe_persist_async()  # schedules write #1 (in a thread)
    await asyncio.sleep(0.05)
    # while write #1 is mid-flight, a second request must NOT start a concurrent write
    eng._last_persist_ts = 0.0  # bypass the time debounce to isolate the single-flight guard
    eng._mark_dirty()
    await eng._maybe_persist_async()
    await asyncio.sleep(0.05)
    assert started["n"] == 1  # single-flight: second write did not start while first in flight
    await asyncio.sleep(0.6)  # let the first finish


@pytest.mark.asyncio
async def test_mark_to_market_no_positions_does_not_block(tmp_path: Path, monkeypatch):
    """mark_to_market ללא פוזיציות לא כותב 6MB סינכרונית בכל קריאה (debounced)."""
    eng = DemoEngine(state_path=tmp_path / "s.json")
    eng.state = DemoState(balance_usd=1000.0, positions=[], trades=[], equity_history=[])
    writes = {"n": 0}
    monkeypatch.setattr(demo_engine, "atomic_write_json",
                        lambda *a, **k: writes.__setitem__("n", writes["n"] + 1))
    for _ in range(10):
        await eng.mark_to_market()
    await asyncio.sleep(0.2)
    assert writes["n"] <= 1  # 10 rapid marks → at most ONE debounced write (not 10 synchronous 6MB saves)
