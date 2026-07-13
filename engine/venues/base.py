# engine/venues/base.py
"""Venue seam: an interface the strategy runner talks to so ORDER placement (and the
market discovery + order book it depends on) can be routed per order_venue, while every
strategy decision path stays identical. See docs/superpowers/plans/2026-07-13-m2a-...md."""
from __future__ import annotations

import abc
from typing import Any, Optional

from market_discovery import ActiveMarket  # re-export the neutral market model (do NOT fork)

VALID_ORDER_VENUES: tuple[str, str] = ("polymarket", "predict_fun")
_DEFAULT = "polymarket"


def normalize(value) -> str:
    return value if value in VALID_ORDER_VENUES else _DEFAULT


class Venue(abc.ABC):
    # --- identity / UI ---
    name: str
    is_testnet: bool
    collateral: str   # "USDC" | "USDT"
    chain_id: int

    # --- market discovery + book (async) ---
    @abc.abstractmethod
    async def discover_active_window(self, window: str) -> Optional[ActiveMarket]: ...
    @abc.abstractmethod
    async def best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]: ...
    @abc.abstractmethod
    async def get_book(self, client: Any, token_id: str) -> dict: ...

    # --- orders + portfolio (async) ---
    @abc.abstractmethod
    async def place_entry_order(self, token_id: str, contracts: float, price: float, side: str,
                                *, order_mode: str = "limit", entry_slippage_pct: float = 2.0) -> dict: ...
    @abc.abstractmethod
    async def place_exit_order(self, token_id: str, contracts: float, bid: float,
                               *, order_mode: str = "limit", exit_slippage_pct: float = 5.0,
                               retry_max_attempts: int = 3) -> dict: ...
    @abc.abstractmethod
    async def fetch_portfolio(self, *, force: bool = False) -> dict: ...
    @abc.abstractmethod
    async def fetch_chain_shares_for_token(self, token_id: str) -> Optional[float]: ...

    # --- account / lifecycle (SYNC — match live_clob) ---
    @abc.abstractmethod
    def fetch_account(self) -> dict: ...
    @abc.abstractmethod
    def reset_caches(self) -> None: ...
    @abc.abstractmethod
    def live_disabled_reason(self) -> Optional[str]: ...
