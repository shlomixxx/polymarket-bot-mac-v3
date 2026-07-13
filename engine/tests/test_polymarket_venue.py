# engine/tests/test_polymarket_venue.py
"""M2a Task 2: PolymarketVenue (pure delegation) + runner routes through self._venue.

Every method must forward args unchanged and return the wrapped live_clob/market_discovery
result object UNCHANGED (identity-checked via `is sentinel`, not just equality) — this is a
pure refactor, byte-for-byte Polymarket behavior must be preserved.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from venues.polymarket import PolymarketVenue


def test_identity_props():
    v = PolymarketVenue()
    assert (v.name, v.is_testnet, v.collateral, v.chain_id) == ("polymarket", False, "USDC", 137)


@pytest.mark.asyncio
async def test_place_entry_order_delegates_unchanged():
    v = PolymarketVenue()
    sentinel = {"ok": True, "order_id": "abc", "price": 0.3, "size": 10}
    with patch("live_clob.place_entry_order", AsyncMock(return_value=sentinel)) as m:
        out = await v.place_entry_order("TID", 10, 0.3, "Up", order_mode="market", entry_slippage_pct=2.0)
    assert out is sentinel
    m.assert_awaited_once_with("TID", 10, 0.3, "Up", order_mode="market", entry_slippage_pct=2.0)


@pytest.mark.asyncio
async def test_place_exit_order_delegates_unchanged():
    v = PolymarketVenue()
    sentinel = {"ok": True, "order_id": "xyz", "price": 0.4, "size": 5}
    with patch("live_clob.place_exit_order", AsyncMock(return_value=sentinel)) as m:
        out = await v.place_exit_order("TID2", 5, 0.4, order_mode="limit", exit_slippage_pct=5.0, retry_max_attempts=3)
    assert out is sentinel
    m.assert_awaited_once_with("TID2", 5, 0.4, order_mode="limit", exit_slippage_pct=5.0, retry_max_attempts=3)


@pytest.mark.asyncio
async def test_fetch_portfolio_delegates():
    v = PolymarketVenue()
    sentinel = {"ok": True, "balance_usd": 5.0, "positions": [], "equity_usd": 5.0}
    with patch("live_clob.fetch_live_portfolio", AsyncMock(return_value=sentinel)) as m:
        out = await v.fetch_portfolio(force=True)
    assert out is sentinel
    m.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_fetch_chain_shares_for_token_delegates():
    v = PolymarketVenue()
    with patch("live_clob.fetch_chain_shares_for_token", AsyncMock(return_value=12.5)) as m:
        out = await v.fetch_chain_shares_for_token("TID3")
    assert out == 12.5
    m.assert_awaited_once_with("TID3")


def test_fetch_account_is_sync_and_delegates():
    v = PolymarketVenue()
    sentinel = {"ok": True, "balance_usd": 5.0}
    with patch("live_clob.fetch_polymarket_clob_account", MagicMock(return_value=sentinel)) as m:
        out = v.fetch_account()      # NOT awaited
    assert out is sentinel
    m.assert_called_once_with()


def test_reset_caches_delegates_both():
    v = PolymarketVenue()
    with patch("live_clob.reset_portfolio_cache", MagicMock()) as m1, \
         patch("live_clob.reset_trading_client_cache", MagicMock()) as m2:
        v.reset_caches()
    m1.assert_called_once_with()
    m2.assert_called_once_with()


def test_live_disabled_reason_delegates():
    v = PolymarketVenue()
    with patch("live_clob._live_disabled_reason", MagicMock(return_value="POLYMARKET_LIVE!=1")) as m:
        assert v.live_disabled_reason() == "POLYMARKET_LIVE!=1"
    m.assert_called_once_with()


@pytest.mark.asyncio
async def test_discover_active_window_delegates():
    v = PolymarketVenue()
    sentinel = object()
    with patch("market_discovery.discover_active_btc_window", AsyncMock(return_value=sentinel)) as m:
        out = await v.discover_active_window("5m")
    assert out is sentinel
    m.assert_awaited_once_with("5m")


@pytest.mark.asyncio
async def test_get_book_delegates():
    v = PolymarketVenue()
    sentinel = {"bids": [], "asks": []}
    fake_client = object()
    with patch("market_discovery.get_clob_book", AsyncMock(return_value=sentinel)) as m:
        out = await v.get_book(fake_client, "TID4")
    assert out is sentinel
    m.assert_awaited_once_with(fake_client, "TID4")


@pytest.mark.asyncio
async def test_best_bid_ask_uses_fresh_ws_price_when_available():
    v = PolymarketVenue()

    class _FakeTp:
        def __init__(self, ts):
            self.ts = ts

    import time as time_mod
    fake_stream = MagicMock()
    fake_stream.get_best_bid_ask.return_value = (0.31, 0.33)
    fake_stream.get_price.return_value = _FakeTp(time_mod.time())

    fake_ws_module = MagicMock()
    fake_ws_module.price_stream = fake_stream

    import sys
    with patch.dict(sys.modules, {"ws_price_stream": fake_ws_module}):
        bid, ask = await v.best_bid_ask("TID5")

    assert (bid, ask) == (0.31, 0.33)


@pytest.mark.asyncio
async def test_best_bid_ask_falls_back_to_clob_book_when_ws_stale():
    v = PolymarketVenue()

    class _FakeTp:
        def __init__(self, ts):
            self.ts = ts

    fake_stream = MagicMock()
    fake_stream.get_best_bid_ask.return_value = (0.31, 0.33)
    fake_stream.get_price.return_value = _FakeTp(0.0)  # ancient ts -> stale

    fake_ws_module = MagicMock()
    fake_ws_module.price_stream = fake_stream

    book = {"bids": [{"price": "0.29"}], "asks": [{"price": "0.35"}]}

    import sys
    with patch.dict(sys.modules, {"ws_price_stream": fake_ws_module}), \
         patch("market_discovery.get_clob_book", AsyncMock(return_value=book)):
        bid, ask = await v.best_bid_ask("TID6")

    assert (bid, ask) == (0.29, 0.35)


def test_runner_defaults_to_polymarket_venue_and_can_switch():
    import strategy_runner
    import venues
    from demo_engine import DemoEngine

    r = strategy_runner.StrategyRunner(DemoEngine())
    assert r._venue.name == "polymarket"
    assert r.venue is r._venue
    r.select_venue("predict_fun")
    assert r._venue.name == "predict_fun"
    r.select_venue("polymarket")
    assert r._venue is venues.get_venue("polymarket")
