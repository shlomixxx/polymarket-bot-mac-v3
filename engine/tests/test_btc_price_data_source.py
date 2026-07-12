# engine/tests/test_btc_price_data_source.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import btc_price
import data_source


@pytest.mark.asyncio
async def test_binance_mode_uses_binance_and_labels_binance():
    data_source.set_active("binance")
    try:
        with patch("btc_price.fetch_btc_spot_usdt", AsyncMock(return_value=101_000.0)) as spot:
            price, source = await btc_price.fetch_btc_current_usd()
        assert (price, source) == (101_000.0, "binance")
        spot.assert_awaited_once()
    finally:
        data_source.set_active("polymarket")


@pytest.mark.asyncio
async def test_polymarket_mode_prefers_chainlink_stream():
    data_source.set_active("polymarket")
    fake_stream = type("S", (), {"get_current_price": staticmethod(lambda: {"value": 99_000.0})})()
    with patch.dict("sys.modules", {"chainlink_price_stream": type("M", (), {"chainlink_stream": fake_stream})}):
        price, source = await btc_price.fetch_btc_current_usd()
    assert (price, source) == (99_000.0, "chainlink_stream")
