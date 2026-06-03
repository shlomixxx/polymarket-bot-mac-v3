"""טסטים ל-engine/_cache.py — TTLCache + SingleFlight (תשתית 0.1)."""
import asyncio

import pytest

from _cache import SingleFlight, TTLCache


def test_ttlcache_returns_value_within_ttl():
    c = TTLCache(ttl_sec=10.0)
    c.set("k", 123, now=1000.0)
    assert c.get("k", now=1005.0) == 123


def test_ttlcache_expires_after_ttl():
    c = TTLCache(ttl_sec=10.0)
    c.set("k", 123, now=1000.0)
    assert c.get("k", now=1011.0) is None


def test_ttlcache_missing_key_returns_none():
    c = TTLCache(ttl_sec=10.0)
    assert c.get("nope", now=1.0) is None


def test_ttlcache_invalidate_single_key():
    c = TTLCache(ttl_sec=10.0)
    c.set("a", 1, now=1000.0)
    c.set("b", 2, now=1000.0)
    c.invalidate("a")
    assert c.get("a", now=1000.0) is None
    assert c.get("b", now=1000.0) == 2


def test_ttlcache_invalidate_all():
    c = TTLCache(ttl_sec=10.0)
    c.set("a", 1, now=1000.0)
    c.set("b", 2, now=1000.0)
    c.invalidate()
    assert len(c) == 0


def test_ttlcache_prune_drops_expired():
    c = TTLCache(ttl_sec=10.0)
    c.set("old", 1, now=1000.0)
    c.set("new", 2, now=1009.0)
    c.prune(now=1011.0)
    assert c.get("new", now=1011.0) == 2
    assert len(c) == 1


@pytest.mark.asyncio
async def test_single_flight_coalesces_concurrent_calls():
    """שתי קריאות מקבילות לאותו מפתח -> factory נקרא פעם אחת בלבד."""
    sf = SingleFlight()
    calls = {"n": 0}
    gate = asyncio.Event()

    async def factory():
        calls["n"] += 1
        await gate.wait()
        return "result"

    t1 = asyncio.create_task(sf.do("key", factory))
    t2 = asyncio.create_task(sf.do("key", factory))
    await asyncio.sleep(0.01)  # ודא ששניהם נכנסו ל-inflight
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert r1 == "result"
    assert r2 == "result"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_single_flight_does_not_cache_across_calls():
    """אחרי שהבקשה הסתיימה — קריאה חדשה מפעילה את ה-factory מחדש (אין caching)."""
    sf = SingleFlight()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return calls["n"]

    assert await sf.do("k", factory) == 1
    assert await sf.do("k", factory) == 2
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_single_flight_separate_keys_run_independently():
    sf = SingleFlight()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return calls["n"]

    await asyncio.gather(sf.do("a", factory), sf.do("b", factory))
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_single_flight_propagates_exception_and_recovers():
    """כשל ב-factory מתפשט לכל הממתינים, ואז ה-inflight מתנקה כך שקריאה הבאה מנסה מחדש."""
    sf = SingleFlight()
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await sf.do("k", boom)

    async def ok():
        return "fine"

    assert await sf.do("k", ok) == "fine"
    assert calls["n"] == 1  # הכשל לא נשמר ב-cache
