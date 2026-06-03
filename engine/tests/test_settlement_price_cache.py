"""טסטים ל-cache מחירי settlement (A-7): open/close נרות 1m — immutable אחרי סגירה."""
import time
from unittest.mock import AsyncMock, patch

import pytest

import btc_price


def _reset_caches():
    btc_price._OPEN_PRICE_CACHE.clear()
    btc_price._CLOSE_PRICE_CACHE.clear()


def _mock_client(json_row, calls):
    async def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        r = AsyncMock()
        r.raise_for_status = lambda: None
        r.json = lambda: [json_row]
        return r
    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    return client


@pytest.mark.asyncio
async def test_open_price_cached_by_epoch():
    _reset_caches()
    epoch = 1_700_000_000
    calls = {"n": 0}
    client = _mock_client([epoch * 1000, "111.0", "1", "1", "1", "1"], calls)
    with patch("btc_price._get_binance_client", return_value=client):
        v1 = await btc_price.fetch_open_price_at_window_start(epoch)
        v2 = await btc_price.fetch_open_price_at_window_start(epoch)
    assert v1 == v2 == 111.0
    assert calls["n"] == 1  # נמשך פעם אחת בלבד — השאר מה-cache


@pytest.mark.asyncio
async def test_close_price_none_not_cached():
    """נר עדיין פתוח (closeTime בעתיד) -> None, ואסור לאחסן None — קריאה חוזרת מנסה שוב."""
    _reset_caches()
    epoch, ws = 1_700_000_000, 300
    future_close = int((time.time() + 10_000) * 1000)
    calls = {"n": 0}
    client = _mock_client([0, "1", "2", "3", "98000.0", "5", future_close], calls)
    with patch("btc_price._get_binance_client", return_value=client):
        v1 = await btc_price.fetch_close_price_at_window_end(epoch, ws, max_retries=1)
        v2 = await btc_price.fetch_close_price_at_window_end(epoch, ws, max_retries=1)
    assert v1 is None and v2 is None
    assert calls["n"] == 2  # None לא נשמר -> שתי משיכות


@pytest.mark.asyncio
async def test_close_price_value_cached():
    """נר סגור (closeTime בעבר) -> ערך סופי immutable, נשמר ב-cache."""
    _reset_caches()
    epoch, ws = 1_700_000_000, 300
    past_close = int((time.time() - 10_000) * 1000)
    calls = {"n": 0}
    client = _mock_client([0, "1", "2", "3", "98765.0", "5", past_close], calls)
    with patch("btc_price._get_binance_client", return_value=client):
        v1 = await btc_price.fetch_close_price_at_window_end(epoch, ws, max_retries=1)
        v2 = await btc_price.fetch_close_price_at_window_end(epoch, ws, max_retries=1)
    assert v1 == v2 == 98765.0
    assert calls["n"] == 1  # נמשך פעם אחת, השאר מה-cache
