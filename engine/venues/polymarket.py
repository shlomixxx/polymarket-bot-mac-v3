# engine/venues/polymarket.py
"""PolymarketVenue — thin adapter over the UNCHANGED live_clob.py + market_discovery.py.

Zero behavior change: every method forwards to the existing function and returns its result
unchanged. The only relocated logic is fetch_best_bid_ask (moved out of strategy_runner) —
see best_bid_ask() below, moved verbatim from strategy_runner.fetch_best_bid_ask (lines 505-520
as of the M2a Task 2 extraction; see m2a-code-runner-discovery.md item #1)."""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx

import live_clob
import market_discovery
from .base import ActiveMarket, Venue

# Moved verbatim from strategy_runner.py's module-level _get_book_client()/_BOOK_CLIENT —
# this singleton httpx.AsyncClient is only ever used by best_bid_ask()'s CLOB-book fallback.
_BOOK_CLIENT: Optional[httpx.AsyncClient] = None


def _get_book_client() -> httpx.AsyncClient:
    global _BOOK_CLIENT
    if _BOOK_CLIENT is None:
        _BOOK_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=6.0, write=6.0, pool=6.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _BOOK_CLIENT


class PolymarketVenue(Venue):
    name = "polymarket"
    is_testnet = False
    collateral = "USDC"
    chain_id = 137

    async def discover_active_window(self, window: str) -> Optional[ActiveMarket]:
        return await market_discovery.discover_active_btc_window(window)

    async def best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        # Verbatim body of strategy_runner.fetch_best_bid_ask (lines 505-520) — moved here,
        # `self` added, `get_clob_book`/`_get_book_client` resolved against this module.
        from ws_price_stream import price_stream
        bid, ask = price_stream.get_best_bid_ask(token_id)
        if bid is not None or ask is not None:
            tp = price_stream.get_price(token_id)
            if tp and (time.time() - tp.ts) < 30.0:
                return bid, ask
        try:
            book = await market_discovery.get_clob_book(_get_book_client(), token_id)
        except Exception:
            return bid, ask
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        rest_bid = float(bids[0]["price"]) if bids else None
        rest_ask = float(asks[0]["price"]) if asks else None
        return rest_bid, rest_ask

    async def get_book(self, client: Any, token_id: str) -> dict:
        return await market_discovery.get_clob_book(client, token_id)

    async def place_entry_order(self, token_id, contracts, price, side, *, order_mode="limit", entry_slippage_pct=2.0) -> dict:
        return await live_clob.place_entry_order(token_id, contracts, price, side, order_mode=order_mode, entry_slippage_pct=entry_slippage_pct)

    async def place_exit_order(self, token_id, contracts, bid, *, order_mode="limit", exit_slippage_pct=5.0, retry_max_attempts=3) -> dict:
        return await live_clob.place_exit_order(token_id, contracts, bid, order_mode=order_mode, exit_slippage_pct=exit_slippage_pct, retry_max_attempts=retry_max_attempts)

    async def fetch_portfolio(self, *, force: bool = False) -> dict:
        return await live_clob.fetch_live_portfolio(force=force)

    async def fetch_chain_shares_for_token(self, token_id: str) -> Optional[float]:
        return await live_clob.fetch_chain_shares_for_token(token_id)

    def fetch_account(self) -> dict:
        return live_clob.fetch_polymarket_clob_account()

    def reset_caches(self) -> None:
        live_clob.reset_portfolio_cache()
        live_clob.reset_trading_client_cache()

    def live_disabled_reason(self) -> Optional[str]:
        return live_clob._live_disabled_reason()
