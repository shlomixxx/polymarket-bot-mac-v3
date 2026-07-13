# engine/venues/predict_fun.py
"""PredictFunVenue — Predict.fun (BNB Chain) venue. M2a = READ-ONLY on testnet (discover + book).

Order placement raises until M2b. Testnet base (`https://api-testnet.predict.fun`) needs no API
key for reads; respect the documented 240 req/min.

Field shapes below are taken from a LIVE testnet capture (2026-07-13), not just the docs — see
docs/superpowers/sdd/... research notes `m2-research-market-map.md` / `m2-research-predict-api.md`.
Two things the initial design sketch got wrong, fixed here:
  1. `outcomes[].bestBid`/`bestAsk` are NOT bare floats — they are `null` (no liquidity yet) or a
     `{"price": <0..1>, "size": <shares>}` object. See `_price_of()`.
  2. There is no `minOrderSize` field on the market object (checked the live sample) — Predict's
     docs quote a flat 1 USDT minimum, so we hardcode that instead of reading a phantom key.

Also: `GET /v1/markets/{id}/orderbook` is keyed by the market's numeric `id`, NOT by the outcome's
`onChainId` — and the returned book is priced for the "Yes" (Up) side only ("YES asks == NO bids,
YES bids == NO asks" per docs). `get_book()` looks up which market/outcome a token_id belongs to
and mirrors the Down side at (1 - price) when needed. The inlined `bestBid`/`bestAsk` on each
outcome, by contrast, are already given per-outcome (already mirrored by the API) — no extra work
needed there.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx

from .base import ActiveMarket, Venue

_TESTNET_BASE = "https://api-testnet.predict.fun"
_MAINNET_BASE = "https://api.predict.fun"  # wired for real in M3/M4; unused while is_testnet=True


def _is_testnet() -> bool:
    # Default-safe: only an explicit opt-out points at mainnet.
    return os.environ.get("PREDICT_MAINNET", "").strip() != "1"


def _price_of(value: Any) -> Optional[float]:
    """Normalize outcomes[].bestBid/bestAsk: null | {"price":.., "size":..} | (defensively) a bare
    number, all -> float|None. See module docstring point (1)."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("price")
        if value is None:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PredictFunVenue(Venue):
    name = "predict_fun"
    collateral = "USDT"

    def __init__(self) -> None:
        self._testnet = _is_testnet()

    @property
    def is_testnet(self) -> bool:
        return self._testnet

    @property
    def chain_id(self) -> int:
        return 97 if self._testnet else 56

    @property
    def _base(self) -> str:
        return _TESTNET_BASE if self._testnet else _MAINNET_BASE

    # --- REST reads (mockable seams: tests patch these directly) ---
    async def _get_open_crypto_updown_markets(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                f"{self._base}/v1/markets",
                params={"marketVariant": "CRYPTO_UP_DOWN", "status": "OPEN"},
            )
            r.raise_for_status()
            payload = r.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return data if isinstance(data, list) else []

    async def _get_orderbook(self, market_id: Any) -> dict:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{self._base}/v1/markets/{market_id}/orderbook")
            r.raise_for_status()
            payload = r.json()
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    async def _find_market_and_outcome(self, token_id: str) -> Optional[tuple[dict, dict]]:
        """Locate the market + outcome object whose onChainId == token_id (by value, not index)."""
        markets = await self._get_open_crypto_updown_markets()
        for mk in markets:
            for o in mk.get("outcomes", []):
                if str(o.get("onChainId")) == str(token_id):
                    return mk, o
        return None

    # --- Venue interface: discovery + book ---
    async def discover_active_window(self, window: str) -> Optional[ActiveMarket]:
        want_sec = 900 if window == "15m" else 300
        prefix = "btc-updown-15m-" if window == "15m" else "btc-updown-5m-"
        markets = await self._get_open_crypto_updown_markets()

        candidates: list[tuple[int, dict]] = []
        for mk in markets:
            slug = str(mk.get("categorySlug", ""))
            if not slug.startswith(prefix):
                continue
            try:
                epoch = int(slug.rsplit("-", 1)[-1])
            except ValueError:
                continue
            candidates.append((epoch, mk))
        if not candidates:
            return None

        # Prefer the window that's live right now; else the nearest upcoming one; else whatever
        # the API gave us (keeps this robust for a mocked single-market response in tests).
        now = int(time.time())
        live = [c for c in candidates if c[0] <= now < c[0] + want_sec]
        if live:
            epoch, mk = live[0]
        else:
            upcoming = [c for c in candidates if c[0] > now]
            epoch, mk = min(upcoming, key=lambda c: c[0]) if upcoming else candidates[0]

        outs = {str(o.get("name", "")).lower(): o for o in mk.get("outcomes", [])}
        up, down = outs.get("up"), outs.get("down")
        if not up or not down:
            return None  # never guess a side from array position

        up_price = _price_of(up.get("bestAsk"))
        down_price = _price_of(down.get("bestAsk"))
        return ActiveMarket(
            slug=str(mk.get("categorySlug", "")),
            epoch=epoch,
            condition_id=str(mk.get("conditionId", "")),
            end_date_iso=str(mk.get("boostEndsAt") or mk.get("endDate") or ""),
            closed=(str(mk.get("tradingStatus", "OPEN")).upper() != "OPEN"),
            token_up=str(up.get("onChainId")),
            token_down=str(down.get("onChainId")),
            outcome_prices=(
                up_price if up_price is not None else 0.0,
                down_price if down_price is not None else 0.0,
            ),
            order_min_size=1.0,  # flat per docs; no per-market min-size key on the live testnet object
            title=str(mk.get("question") or mk.get("title") or ""),
            window_sec=want_sec,
            order_min_size_source="gamma",  # not fetched from an authoritative book endpoint
            resolution_source="predict.fun CRYPTO_UP_DOWN (ChainlinkUpDownAdapter / Pyth)",
        )

    async def best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        # bestBid/bestAsk are already inlined PER OUTCOME on the market object (already mirrored
        # for Down by the API) — no separate book call needed for top-of-book.
        found = await self._find_market_and_outcome(token_id)
        if found is None:
            return (None, None)
        _, outcome = found
        return _price_of(outcome.get("bestBid")), _price_of(outcome.get("bestAsk"))

    async def get_book(self, client: Any, token_id: str) -> dict:
        found = await self._find_market_and_outcome(token_id)
        if found is None:
            return {"bids": [], "asks": []}
        mk, outcome = found
        book = await self._get_orderbook(mk.get("id"))
        raw_bids = book.get("bids") or []
        raw_asks = book.get("asks") or []
        if str(outcome.get("name", "")).lower() == "down":
            # Full-depth book is Yes(Up)-priced only; Down = mirror at (1 - price).
            bids = [(round(1.0 - float(p), 6), q) for p, q in raw_asks]
            asks = [(round(1.0 - float(p), 6), q) for p, q in raw_bids]
        else:
            bids = [(float(p), q) for p, q in raw_bids]
            asks = [(float(p), q) for p, q in raw_asks]
        return {
            "bids": [{"price": p, "size": float(q)} for p, q in bids],
            "asks": [{"price": p, "size": float(q)} for p, q in asks],
        }

    # --- orders: NOT in M2a (M2b) ---
    def _order_guard(self):
        raise NotImplementedError(
            "PredictFunVenue order placement is M2b (testnet-first, behind the triple lock)."
        )

    async def place_entry_order(self, token_id: str, contracts: float, price: float, side: str,
                                 *, order_mode: str = "limit", entry_slippage_pct: float = 2.0) -> dict:
        self._order_guard()

    async def place_exit_order(self, token_id: str, contracts: float, bid: float,
                                *, order_mode: str = "limit", exit_slippage_pct: float = 5.0,
                                retry_max_attempts: int = 3) -> dict:
        self._order_guard()

    async def fetch_portfolio(self, *, force: bool = False) -> dict:
        return {
            "ok": False, "error": "predict_fun portfolio is M2b", "balance_usd": 0.0,
            "positions": [], "equity_usd": 0.0, "address": None, "funder_address": None,
            "is_proxy": False, "hint": "testnet trading not yet enabled",
        }

    async def fetch_chain_shares_for_token(self, token_id: str) -> Optional[float]:
        return None

    def fetch_account(self) -> dict:
        return {"ok": False, "error": "predict_fun account is M2b"}

    def reset_caches(self) -> None:
        return None

    def live_disabled_reason(self) -> Optional[str]:
        if os.environ.get("PREDICT_LIVE", "").strip() != "1":
            return "PREDICT_LIVE != '1' (testnet only)"
        if not os.environ.get("PREDICT_PRIVATE_KEY", "").strip():
            return "PREDICT_PRIVATE_KEY not set"
        return None
