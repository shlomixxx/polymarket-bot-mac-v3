# engine/tests/test_venues_base.py
"""Venue registry + interface contract."""
from __future__ import annotations
import inspect
import pytest
import venues
from venues import base


def test_valid_order_venues_and_normalize():
    assert base.VALID_ORDER_VENUES == ("polymarket", "predict_fun")
    assert base.normalize("predict_fun") == "predict_fun"
    assert base.normalize("nasdaq") == "polymarket"
    assert base.normalize(None) == "polymarket"


def test_active_market_is_reexported_not_forked():
    import market_discovery
    assert base.ActiveMarket is market_discovery.ActiveMarket


def test_get_venue_polymarket_singleton():
    v1 = venues.get_venue("polymarket")
    v2 = venues.get_venue("polymarket")
    assert v1 is v2                      # singleton (preserves client caches)
    assert v1.name == "polymarket"
    assert v1.is_testnet is False
    assert v1.collateral == "USDC"


def test_get_venue_unknown_normalizes_to_polymarket():
    assert venues.get_venue("bogus").name == "polymarket"


def test_venue_async_sync_shape():
    v = venues.get_venue("polymarket")
    assert inspect.iscoroutinefunction(v.place_entry_order)
    assert inspect.iscoroutinefunction(v.fetch_portfolio)
    assert inspect.iscoroutinefunction(v.discover_active_window)
    assert not inspect.iscoroutinefunction(v.fetch_account)      # sync
    assert not inspect.iscoroutinefunction(v.reset_caches)       # sync
    assert not inspect.iscoroutinefunction(v.live_disabled_reason)  # sync
