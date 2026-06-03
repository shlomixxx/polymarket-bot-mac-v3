"""טסטים ל-A-5: discovery cache מוחזק לאורך גוף החלון (slug/tokens immutable), עם תפוגת גבול.
ול-B-3: single-flight על get_clob_book."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import market_discovery as md


def _fake_market(epoch: int, window_sec: int):
    # _cached_market ניגש רק ל-.epoch ו-.window_sec
    return SimpleNamespace(epoch=epoch, window_sec=window_sec)


def test_cached_market_held_for_window_body_despite_old_ts():
    """ts ישן (>30s) לא מפקיע יותר — כל עוד החלון פתוח, מחזירים את אותו שוק (A-5)."""
    step = 300
    now = int(time.time())
    epoch = (now // step) * step  # החלון הנוכחי — עדיין פתוח
    am = _fake_market(epoch, step)
    md._DISCOVERY_CACHE["5m"] = (time.time() - 9999.0, am)  # ts ישן מאוד
    try:
        assert md._cached_market("5m") is am
    finally:
        md._DISCOVERY_CACHE.pop("5m", None)


def test_cached_market_expires_at_window_end():
    """אחרי סוף החלון — None, כדי שייעשה re-discovery ל-epoch החדש (rollover)."""
    step = 300
    now = int(time.time())
    epoch = (now // step) * step - step  # החלון הקודם — כבר נסגר
    am = _fake_market(epoch, step)
    md._DISCOVERY_CACHE["5m"] = (time.time(), am)  # ts טרי, אבל החלון נגמר
    try:
        assert md._cached_market("5m") is None
    finally:
        md._DISCOVERY_CACHE.pop("5m", None)


class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"bids": [{"price": "0.4"}], "asks": [{"price": "0.6"}]}


@pytest.mark.asyncio
async def test_get_clob_book_single_flight_coalesces_concurrent():
    """B-3: שתי קריאות מקבילות לאותו token -> בקשת רשת אחת (dedup); כל caller מקבל תוצאה טרייה."""
    calls = {"n": 0}
    gate = asyncio.Event()

    class FakeClient:
        async def get(self, url, params=None, timeout=None):
            calls["n"] += 1
            await gate.wait()
            return _FakeResp()

    client = FakeClient()
    t1 = asyncio.create_task(md.get_clob_book(client, "TOK"))
    t2 = asyncio.create_task(md.get_clob_book(client, "TOK"))
    await asyncio.sleep(0.01)
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert calls["n"] == 1  # בקשה אחת בלבד
    assert r1["asks"][0]["price"] == "0.6"
    assert r2["asks"][0]["price"] == "0.6"

