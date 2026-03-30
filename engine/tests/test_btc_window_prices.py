"""בדיקות fetch_window_start_end_btc_usd (עם mock ל-Binance)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from btc_price import (
    _decode_chainlink_latest_round_answer,
    fetch_close_price_at_window_end,
    fetch_open_price_at_window_start,
    fetch_window_start_end_btc_usd,
)


@pytest.mark.asyncio
async def test_fetch_close_price_uses_last_minute_candle_close():
    epoch = 1_700_000_000
    window_sec = 300
    last_open_ms = (epoch + window_sec - 60) * 1000
    mock_row = [last_open_ms, "1", "2", "3", "98765.43", "6"]

    async def fake_get(url, params=None, timeout=None):
        assert params.get("startTime") == last_open_ms
        assert params.get("limit") == 1
        r = AsyncMock()
        r.raise_for_status = lambda: None
        r.json = lambda: [mock_row]
        return r

    with patch("btc_price.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.get = AsyncMock(side_effect=fake_get)
        client_cls.return_value = client
        out = await fetch_close_price_at_window_end(epoch, window_sec)
    assert out == pytest.approx(98765.43)


def test_decode_chainlink_latest_round_answer():
    # ערך דמה: 65805.19 USD עם 8 ספרות עשרוניות ב-Chainlink
    ans_raw = int(65805.19 * 10**8)
    b = ans_raw.to_bytes(32, "big", signed=True)
    pad = b"\x00" * 32 + b
    hex_data = "0x" + pad.hex()
    out = _decode_chainlink_latest_round_answer(hex_data)
    assert out == pytest.approx(65805.19, rel=1e-9)


@pytest.mark.asyncio
async def test_fetch_window_start_end_combines_start_and_end():
    epoch = 1_700_000_100

    async def mock_open(*a, **k):
        return 100_000.0

    async def mock_end(e, w):
        return 100_050.0

    with (
        patch("btc_price.fetch_open_price_at_window_start", side_effect=mock_open),
        patch("btc_price.fetch_close_price_at_window_end", side_effect=mock_end),
    ):
        d = await fetch_window_start_end_btc_usd(epoch, 300)
    assert d["start"] == 100_000.0
    assert d["end"] == 100_050.0
    assert d["source"] == "binance_1m_proxy"
