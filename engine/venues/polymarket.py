# engine/venues/polymarket.py
"""Polymarket Venue — MINIMAL SKELETON (Task 1 of M2a).

Satisfies the `Venue` interface shape so `venues.get_venue("polymarket")` works and
Task 1's tests pass. Real bodies (delegating to market_discovery / live_clob) are
filled in during Task 2 — see docs/superpowers/plans/2026-07-13-m2a-...md.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ActiveMarket, Venue


class PolymarketVenue(Venue):
    name = "polymarket"
    is_testnet = False
    collateral = "USDC"
    chain_id = 137

    async def discover_active_window(self, window: str) -> Optional[ActiveMarket]:
        raise NotImplementedError  # filled in Task 2

    async def best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        raise NotImplementedError  # filled in Task 2

    async def get_book(self, client: Any, token_id: str) -> dict:
        raise NotImplementedError  # filled in Task 2

    async def place_entry_order(self, token_id: str, contracts: float, price: float, side: str,
                                *, order_mode: str = "limit", entry_slippage_pct: float = 2.0) -> dict:
        raise NotImplementedError  # filled in Task 2

    async def place_exit_order(self, token_id: str, contracts: float, bid: float,
                               *, order_mode: str = "limit", exit_slippage_pct: float = 5.0,
                               retry_max_attempts: int = 3) -> dict:
        raise NotImplementedError  # filled in Task 2

    async def fetch_portfolio(self, *, force: bool = False) -> dict:
        raise NotImplementedError  # filled in Task 2

    async def fetch_chain_shares_for_token(self, token_id: str) -> Optional[float]:
        raise NotImplementedError  # filled in Task 2

    def fetch_account(self) -> dict:
        raise NotImplementedError  # filled in Task 2

    def reset_caches(self) -> None:
        raise NotImplementedError  # filled in Task 2

    def live_disabled_reason(self) -> Optional[str]:
        raise NotImplementedError  # filled in Task 2
