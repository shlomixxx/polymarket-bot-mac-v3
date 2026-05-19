"""בדיקות עמידות ל־market_discovery: stale-on-error, CLOB cache, warmer.

מוודא שהבאנר ‎"Gamma discovery timeout"‎ לא יחזור גם אם Polymarket מאט
פתאום או כשה־TTL פג ברגע הלא נכון."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest

import market_discovery as md


def _reset_module_state() -> None:
    md._DISCOVERY_CACHE.clear()
    md._DISCOVERY_LOCKS.clear()
    md._CLOB_MIN_SIZE_CACHE.clear()


def _make_market(epoch: int = 1700000000, window_sec: int = 300) -> md.ActiveMarket:
    return md.ActiveMarket(
        slug=f"btc-updown-5m-{epoch}",
        epoch=epoch,
        condition_id="0x1",
        end_date_iso="",
        closed=False,
        token_up="t-up",
        token_down="t-down",
        outcome_prices=(0.5, 0.5),
        order_min_size=5.0,
        title="BTC test",
        window_sec=window_sec,
        order_min_size_source="gamma",
    )


def test_stale_market_returned_when_window_open():
    _reset_module_state()
    now = time.time()
    am = _make_market(epoch=int(now) - 60, window_sec=300)
    md._DISCOVERY_CACHE["5m"] = (now - 999.0, am)
    out = md._stale_market_if_window_open("5m")
    assert out is am


def test_stale_market_none_when_window_closed():
    _reset_module_state()
    now = time.time()
    am = _make_market(epoch=int(now) - 600, window_sec=300)  # נסגר לפני 5 דקות
    md._DISCOVERY_CACHE["5m"] = (now - 30.0, am)
    out = md._stale_market_if_window_open("5m")
    assert out is None


def test_discover_returns_stale_when_gamma_times_out(monkeypatch):
    """כש־discovery הפנימי כושל — צריך להחזיר את הקאש האחרון אם החלון עוד פתוח."""
    _reset_module_state()
    now = time.time()
    am = _make_market(epoch=int(now) - 30, window_sec=300)
    # קאש קיים אבל פג ה־TTL
    md._DISCOVERY_CACHE["5m"] = (now - 999.0, am)

    async def fake_uncached(_window):
        await asyncio.sleep(0.5)
        return None  # מדמה כשל

    monkeypatch.setattr(md, "_discover_uncached", fake_uncached)

    result = asyncio.run(md.discover_active_btc_window("5m"))
    assert result is am, "צריך להחזיר את הקאש האחרון בתור stale fallback"


def test_discover_returns_none_when_no_cache_and_gamma_fails(monkeypatch):
    _reset_module_state()

    async def fake_uncached(_window):
        return None

    monkeypatch.setattr(md, "_discover_uncached", fake_uncached)
    result = asyncio.run(md.discover_active_btc_window("5m"))
    assert result is None


def test_clob_min_size_cache_skips_repeat_book_fetch(monkeypatch):
    """אחרי החדרה ראשונה ל־cache, apply_clob_order_min_size לא קורא יותר ל־book."""
    _reset_module_state()
    am1 = _make_market()
    am2 = _make_market(epoch=1700000300)

    calls = {"n": 0}

    async def fake_get_book(_client, _token_id):
        calls["n"] += 1
        return {"min_order_size": "9", "bids": [], "asks": []}

    monkeypatch.setattr(md, "get_clob_book", fake_get_book)

    asyncio.run(md.apply_clob_order_min_size(am1, None))
    assert am1.order_min_size == 9.0
    assert am1.order_min_size_source == "clob"
    assert calls["n"] == 1

    asyncio.run(md.apply_clob_order_min_size(am2, None))
    assert am2.order_min_size == 9.0
    assert am2.order_min_size_source == "clob"
    assert calls["n"] == 1, "טוקן זהה צריך להחזיר מ־cache בלי קריאה נוספת ל־book"


def test_apply_clob_order_min_size_swallows_timeout(monkeypatch):
    _reset_module_state()
    am = _make_market()

    async def slow_book(_client, _token_id):
        await asyncio.sleep(2.0)
        return {"min_order_size": "11"}

    monkeypatch.setattr(md, "get_clob_book", slow_book)
    asyncio.run(md.apply_clob_order_min_size(am, None, timeout=0.05))
    assert am.order_min_size == 5.0  # נשאר מערך Gamma — לא נדרס בעת timeout
    assert am.order_min_size_source == "gamma"


def test_warmer_loop_calls_discover_periodically(monkeypatch):
    _reset_module_state()
    counter = {"n": 0}

    async def fake_discover(window):
        counter["n"] += 1
        return None

    monkeypatch.setattr(md, "discover_active_btc_window", fake_discover)

    async def runner():
        task = asyncio.create_task(
            md.discovery_warmer_loop(lambda: "5m", interval_sec=0.05)
        )
        # ה־warmer ישן 0.5s לפני הסיבוב הראשון — לתת לו זמן לבצע ≥2 סיבובים
        await asyncio.sleep(0.75)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(runner())
    assert counter["n"] >= 2, "ה־warmer חייב להריץ discover יותר מפעם אחת בפרק זמן קצר"


def test_warmer_loop_continues_after_exception(monkeypatch):
    _reset_module_state()
    counter = {"n": 0}

    async def flaky_discover(window):
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(md, "discover_active_btc_window", flaky_discover)

    async def runner():
        task = asyncio.create_task(
            md.discovery_warmer_loop(lambda: "5m", interval_sec=0.05)
        )
        await asyncio.sleep(0.75)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(runner())
    assert counter["n"] >= 2, "ה־warmer לא נופל גם אם discover זרק שגיאה פעם אחת"
