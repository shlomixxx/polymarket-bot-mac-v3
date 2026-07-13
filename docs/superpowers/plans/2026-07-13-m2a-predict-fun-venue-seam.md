# M2a — Venue seam + read-only Predict.fun (testnet) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Introduce a `Venue` seam so `order_venue = "polymarket" | "predict_fun"` routes order placement + the market discovery/book it depends on, while every strategy decision path stays byte-for-byte identical; and add a **read-only** `PredictFunVenue` on BNB testnet (no orders yet).

**Architecture:** New `engine/venues/` package: a `Venue` interface, `PolymarketVenue` (pure delegation to the unchanged `live_clob.py` + `market_discovery.py` + the moved `fetch_best_bid_ask`), a `get_venue` singleton registry, and a read-only `PredictFunVenue` (testnet REST discovery/book; order methods raise until M2b). `StrategyRunner` gets `self._venue` + `select_venue()` and routes ~15 call sites through it. A persisted `order_venue` field mirrors `data_source` exactly.

**Tech Stack:** Python 3 / FastAPI / httpx (engine); pytest + pytest-asyncio + unittest.mock; the `predict-sdk` (PyPI) is NOT needed for M2a (read-only — plain httpx REST).

## Global Constraints

- Default `order_venue = "polymarket"` (verbatim). Allowed values exactly `"polymarket"` | `"predict_fun"`; invalid → HTTP 400 (API) / normalize to `"polymarket"` (registry).
- **This is a refactor of the real-money order path. `data_source="polymarket"` / `order_venue="polymarket"` behavior MUST be byte-for-byte identical to before this branch.** Any change to Polymarket behavior is a defect.
- Preserve async-ness EXACTLY: `discover`, `best_bid_ask`, `get_book`, `place_entry_order`, `place_exit_order`, `fetch_portfolio`, `fetch_chain_shares_for_token` are **async**; `fetch_account`, `reset_caches`, `live_disabled_reason` are **sync**. A caller that did `await live_clob.X(...)` must become `await self._venue.X(...)`; a sync caller stays sync.
- `live_clob.py`, `market_discovery.py`, `demo_engine.py`, `data_source.py` are NOT modified (PolymarketVenue delegates to them). The ONLY exception: `strategy_runner.py`'s `fetch_best_bid_ask` body MOVES into `PolymarketVenue`.
- No new order placement in M2a. `PredictFunVenue` order methods raise `NotImplementedError` gated by `live_disabled_reason()`. No real money anywhere.
- Predict.fun testnet: REST base `https://api-testnet.predict.fun`, chain 97, NO API key. Respect 240 req/min. Match Up/Down outcome → `onChainId` **by outcome NAME, never array index**.
- Engine tests live in `engine/tests/test_*.py`; run `python3 -m pytest engine/tests/<file> -v` from repo root. `conftest.py` puts `engine/` on `sys.path` (import bare modules).
- Verbatim code references for existing signatures/sites are in these files (read them):
  `.../scratchpad/m2a-code-live-clob.md`, `.../scratchpad/m2a-code-runner-discovery.md`, `.../scratchpad/m2a-code-config.md`.

---

### Task 1: `engine/venues/` package — interface, registry, `PolymarketVenue` skeleton

**Files:**
- Create: `engine/venues/__init__.py`, `engine/venues/base.py`
- Test: `engine/tests/test_venues_base.py`

**Interfaces:**
- Produces:
  - `base.VALID_ORDER_VENUES = ("polymarket", "predict_fun")`, `base.normalize(v) -> str` (invalid/None → `"polymarket"`).
  - `base.Venue` — an `abc.ABC` (or `typing.Protocol`) declaring: async `discover_active_window(window)`, async `best_bid_ask(token_id)`, async `get_book(client, token_id)`, async `place_entry_order(token_id, contracts, price, side, *, order_mode, entry_slippage_pct)`, async `place_exit_order(token_id, contracts, bid, *, order_mode, exit_slippage_pct, retry_max_attempts)`, async `fetch_portfolio(*, force=False)`, async `fetch_chain_shares_for_token(token_id)`, sync `fetch_account()`, sync `reset_caches()`, sync `live_disabled_reason()`, and read-only props `name`, `is_testnet`, `collateral`, `chain_id`.
  - `base.ActiveMarket` — re-exported from `market_discovery` (NOT forked).
  - `__init__.get_venue(name) -> Venue` — singleton per venue; `VALID_ORDER_VENUES`, `normalize` re-exported.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest engine/tests/test_venues_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'venues'`.

- [ ] **Step 3: Create `engine/venues/base.py`**

```python
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
```

- [ ] **Step 4: Create `engine/venues/__init__.py`** (registry; `PolymarketVenue` import deferred so Task 1 can pass before Task 2 — use a lazy import)

```python
# engine/venues/__init__.py
"""get_venue(name) -> singleton Venue. Singletons preserve live_clob's client/portfolio caches."""
from __future__ import annotations

from .base import Venue, ActiveMarket, VALID_ORDER_VENUES, normalize

_INSTANCES: dict[str, Venue] = {}


def get_venue(name: str) -> Venue:
    key = normalize(name)
    inst = _INSTANCES.get(key)
    if inst is None:
        if key == "predict_fun":
            from .predict_fun import PredictFunVenue
            inst = PredictFunVenue()
        else:
            from .polymarket import PolymarketVenue
            inst = PolymarketVenue()
        _INSTANCES[key] = inst
    return inst
```

> Task 1 note: `get_venue("polymarket")` imports `.polymarket`, which doesn't exist until Task 2. To keep Task 1 green in isolation, create a MINIMAL `engine/venues/polymarket.py` stub now that satisfies the interface with `name/is_testnet/collateral/chain_id` set and all methods present (delegating bodies filled in Task 2). Include the same minimal stub for `engine/venues/predict_fun.py` (methods raise `NotImplementedError`) so the registry imports cleanly. The stub's async/sync signatures MUST match `base.Venue` exactly (Task 1's `test_venue_async_sync_shape` checks this).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest engine/tests/test_venues_base.py -v`
Expected: PASS (5 tests). Then `python3 -m pytest engine/tests/ -q` (no regressions — this task adds files only).

- [ ] **Step 6: Commit**

```bash
git add engine/venues/ engine/tests/test_venues_base.py
git commit -m "feat(venues): Venue interface + get_venue registry + skeletons"
```

---

### Task 2: `PolymarketVenue` (pure delegation) + reroute the runner through `self._venue`

**Files:**
- Modify: `engine/venues/polymarket.py` (fill the delegating bodies; move `fetch_best_bid_ask` here)
- Modify: `engine/strategy_runner.py` (add `self._venue` + `select_venue`; reroute ~15 call sites; remove the moved `fetch_best_bid_ask` body; adjust imports)
- Test: `engine/tests/test_polymarket_venue.py`

**Interfaces:**
- Consumes: `base.Venue`, and the unchanged `live_clob` + `market_discovery` functions (see `m2a-code-live-clob.md` / `m2a-code-runner-discovery.md` for exact signatures + result-dict keys).
- Produces: `PolymarketVenue` whose every method returns the SAME object the wrapped function returns (byte-identical); `StrategyRunner._venue` (default `get_venue("polymarket")`) + `StrategyRunner.select_venue(name)`.

- [ ] **Step 1: Write the failing test** (delegation must forward args and return the wrapped result unchanged)

```python
# engine/tests/test_polymarket_venue.py
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
async def test_fetch_portfolio_delegates():
    v = PolymarketVenue()
    sentinel = {"ok": True, "balance_usd": 5.0, "positions": [], "equity_usd": 5.0}
    with patch("live_clob.fetch_live_portfolio", AsyncMock(return_value=sentinel)) as m:
        out = await v.fetch_portfolio(force=True)
    assert out is sentinel
    m.assert_awaited_once_with(force=True)


def test_fetch_account_is_sync_and_delegates():
    v = PolymarketVenue()
    sentinel = {"ok": True, "balance_usd": 5.0}
    with patch("live_clob.fetch_polymarket_clob_account", MagicMock(return_value=sentinel)) as m:
        out = v.fetch_account()      # NOT awaited
    assert out is sentinel
    m.assert_called_once_with()


def test_live_disabled_reason_delegates():
    v = PolymarketVenue()
    with patch("live_clob._live_disabled_reason", MagicMock(return_value="POLYMARKET_LIVE!=1")) as m:
        assert v.live_disabled_reason() == "POLYMARKET_LIVE!=1"
    m.assert_called_once_with()
```

Also add a runner test:
```python
# append to engine/tests/test_polymarket_venue.py
def test_runner_defaults_to_polymarket_venue_and_can_switch():
    import strategy_runner, venues
    r = strategy_runner.StrategyRunner()
    assert r._venue.name == "polymarket"
    r.select_venue("predict_fun")
    assert r._venue.name == "predict_fun"
    r.select_venue("polymarket")
    assert r._venue is venues.get_venue("polymarket")
```
> If `StrategyRunner()` needs constructor args, mirror how the existing tests build it (grep `engine/tests` for `StrategyRunner(`); reuse that exact construction.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest engine/tests/test_polymarket_venue.py -v`
Expected: FAIL — delegating bodies not implemented / `_venue` attr missing.

- [ ] **Step 3a: Fill `engine/venues/polymarket.py`** (pure delegation; move the `fetch_best_bid_ask` body from `strategy_runner.py:505-520` here VERBATIM, adapting `self`):

```python
# engine/venues/polymarket.py
"""PolymarketVenue — thin adapter over the UNCHANGED live_clob.py + market_discovery.py.
Zero behavior change: every method forwards to the existing function and returns its result
unchanged. The only relocated logic is fetch_best_bid_ask (moved out of strategy_runner)."""
from __future__ import annotations
from typing import Any, Optional

import live_clob
import market_discovery
from .base import Venue, ActiveMarket


class PolymarketVenue(Venue):
    name = "polymarket"
    is_testnet = False
    collateral = "USDC"
    chain_id = 137

    async def discover_active_window(self, window: str) -> Optional[ActiveMarket]:
        return await market_discovery.discover_active_btc_window(window)

    async def best_bid_ask(self, token_id: str):
        # <<< MOVE the verbatim body of strategy_runner.fetch_best_bid_ask (lines 505-520) here,
        #     replacing any module-local references as needed. See m2a-code-runner-discovery.md. >>>
        ...

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
```
> IMPORTANT: verify the exact keyword vs positional call convention of each `live_clob.*` function against `m2a-code-live-clob.md` and match it (e.g. `place_entry_order`'s `side` is positional; `order_mode`/`*_slippage_pct` are keyword-only). The result object must be returned **unchanged** (`return await ...`), never rebuilt.

- [ ] **Step 3b: Wire the runner** (`engine/strategy_runner.py`):
1. In `StrategyRunner.__init__` (~523-532) add: `import venues` at top-of-file with the other imports, and in `__init__`: `self._venue = venues.get_venue("polymarket")`. Add a method:
   ```python
   @property
   def venue(self):
       return self._venue
   def select_venue(self, name: str) -> None:
       self._venue = venues.get_venue(name)
   ```
2. Delete the `fetch_best_bid_ask` definition (505-520) — it now lives in `PolymarketVenue`. Replace ALL 8 call sites (646, 900, 1669, 1670, 1863, 2003, 2391, 2433) `fetch_best_bid_ask(<args>)` → `await self._venue.best_bid_ask(<token arg>)`. (Confirm each call's arg is the token id; keep the `await`.)
3. Replace the 8 order/portfolio `live_clob.*` sites with `self._venue.*` KEEPING `await`:
   - `live_clob.place_exit_order(` @651, @2006 → `self._venue.place_exit_order(`
   - `live_clob.fetch_live_portfolio(` @755 → `self._venue.fetch_portfolio(`
   - `live_clob.place_entry_order(` @810, @906, @2213, @2653 → `self._venue.place_entry_order(`
   - `live_clob.fetch_chain_shares_for_token(` @2040 → `self._venue.fetch_chain_shares_for_token(`
4. Replace discovery: `discover_active_btc_window(...)` @1350, @1424 → `await self._venue.discover_active_window(...)`. (Check whether the originals were awaited; keep identical await semantics.)
5. Imports (line 15, 17): drop `import live_clob` ONLY if no direct `live_clob.` references remain (grep to confirm — `_live_trading_ok` may still reference it; if so keep the import). Keep `seconds_until_window_end` imported from `market_discovery` (it stays a free function); remove `discover_active_btc_window`/`get_clob_book` from that import line only if the runner no longer calls them directly.

> Do NOT change `_live_trading_ok()` in this task (that becomes venue-aware in M2b). Do NOT touch strategy math, sizing, DCA, FLW, chop, TP/floor/peak.

- [ ] **Step 4: Run tests + verify byte-identical Polymarket behavior**

Run: `python3 -m pytest engine/tests/test_polymarket_venue.py -v` → PASS.
Run the FULL suite: `python3 -m pytest engine/tests/ -q` → all still pass (this is a pure refactor; any change in count/pass is a red flag to investigate).
Grep-verify no orphan refs: `grep -n "fetch_best_bid_ask\|live_clob.place_\|live_clob.fetch_live\|live_clob.fetch_chain\|discover_active_btc_window" engine/strategy_runner.py` — every remaining hit must be intentional (import lines only, or none).

- [ ] **Step 5: Commit**

```bash
git add engine/venues/polymarket.py engine/strategy_runner.py engine/tests/test_polymarket_venue.py
git commit -m "refactor(venues): PolymarketVenue + route runner order/discovery through self.venue"
```

---

### Task 3: `order_venue` persisted config field + `/api/order-venue` (mirror `data_source`)

**Files:**
- Modify: `engine/strategy_runner.py` (StrategyConfig), `engine/main.py` (ConfigBody, POST validation+apply, persist, load, GET, endpoint)
- Test: `engine/tests/test_order_venue_config.py`

**Interfaces:**
- Consumes: `venues.VALID_ORDER_VENUES`, `runner.select_venue` (Task 2).
- Produces: persisted `StrategyConfig.order_venue` (default `"polymarket"`); `GET/POST /api/order-venue`; a config POST that sets `order_venue` also calls `runner.select_venue(...)`; partial config saves DON'T revert it (mirror the M1 `model_fields_set` fix).

- [ ] **Step 1: Write the failing test** (mirror `engine/tests/test_data_source_config.py` + `test_data_source_endpoint.py`, incl. the tmp_path `CONFIG_PERSISTED_PATH` isolation)

```python
# engine/tests/test_order_venue_config.py
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PERSISTED_PATH", tmp_path / "config_persisted.json")
    yield TestClient(main.app)
    main.runner.rt.config.order_venue = "polymarket"
    main.runner.select_venue("polymarket")


def test_default_is_polymarket(client):
    assert client.get("/api/strategy/config").json()["order_venue"] == "polymarket"
    assert client.get("/api/order-venue").json() == {"order_venue": "polymarket"}


def test_post_endpoint_switches_and_selects_venue(client):
    r = client.post("/api/order-venue", json={"order_venue": "predict_fun"})
    assert r.status_code == 200 and r.json() == {"ok": True, "order_venue": "predict_fun"}
    assert main.runner.rt.config.order_venue == "predict_fun"
    assert main.runner._venue.name == "predict_fun"


def test_invalid_rejected(client):
    assert client.post("/api/order-venue", json={"order_venue": "kraken"}).status_code == 400


def test_partial_config_save_keeps_order_venue(client):
    client.post("/api/order-venue", json={"order_venue": "predict_fun"})
    body = main.ConfigBody().model_dump(); body.pop("order_venue", None)
    assert client.post("/api/strategy/config", json=body).status_code == 200
    assert main.runner.rt.config.order_venue == "predict_fun"   # NOT reverted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest engine/tests/test_order_venue_config.py -v` → FAIL (`order_venue` unknown / endpoint 404).

- [ ] **Step 3: Apply the edits** (each mirrors the `data_source` site — see `m2a-code-config.md` for exact current lines):
1. `strategy_runner.py:109` (beside `data_source`): `order_venue: Literal["polymarket", "predict_fun"] = "polymarket"`.
2. `main.py:1613` (beside `ConfigBody.data_source`): `order_venue: str = "polymarket"`.
3. POST `/api/strategy/config`: after the `data_source` validation (~1661-1662) add
   `if "order_venue" in body.model_fields_set and body.order_venue not in ("polymarket", "predict_fun"): raise HTTPException(400, "order_venue must be 'polymarket' or 'predict_fun'")`.
   In the generic setattr loop, add `order_venue` to the skip beside `data_source` (~1699-1700). In the explicit block (~1710-1713) after setting `data_source`, add:
   `if "order_venue" in body.model_fields_set: runner.rt.config.order_venue = body.order_venue; runner.select_venue(runner.rt.config.order_venue)`.
   Add `"order_venue": runner.rt.config.order_venue` to the `saved` dict (~1715).
4. `_save_persisted_config` (~437): `"order_venue": str(getattr(c, "order_venue", "polymarket")),`. In `_load_persisted_config`, beside the `_data_source.set_active(...)` resync (~382-383): `runner.select_venue(getattr(runner.rt.config, "order_venue", "polymarket"))`.
5. `get_strategy_config` (~1771): `"order_venue": str(getattr(c, "order_venue", "polymarket")),`.
6. Endpoint (near `/api/data-source`, ~1848-1865):
   ```python
   class OrderVenueBody(BaseModel):
       order_venue: str

   @app.get("/api/order-venue")
   async def get_order_venue():
       return {"order_venue": getattr(runner.rt.config, "order_venue", "polymarket")}

   @app.post("/api/order-venue")
   async def set_order_venue(body: OrderVenueBody):
       import venues
       if body.order_venue not in venues.VALID_ORDER_VENUES:
           raise HTTPException(400, "order_venue must be 'polymarket' or 'predict_fun'")
       runner.rt.config.order_venue = body.order_venue  # type: ignore
       runner.select_venue(body.order_venue)
       _save_persisted_config()
       return {"ok": True, "order_venue": runner.rt.config.order_venue}
   ```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest engine/tests/test_order_venue_config.py engine/tests/test_data_source_config.py engine/tests/test_api_smoke.py -q` → all pass. Then full suite `python3 -m pytest engine/tests/ -q`.

- [ ] **Step 5: Commit**

```bash
git add engine/strategy_runner.py engine/main.py engine/tests/test_order_venue_config.py
git commit -m "feat(venues): persist order_venue + /api/order-venue (mirrors data_source)"
```

---

### Task 4: `PredictFunVenue` read-only on BNB testnet (discover + book; orders raise)

**Files:**
- Modify: `engine/venues/predict_fun.py` (fill the read-only bodies; keep order methods raising)
- Test: `engine/tests/test_predict_fun_venue.py`

**Interfaces:**
- Consumes: `base.Venue`, `base.ActiveMarket`, `httpx`.
- Produces: `PredictFunVenue` with working async `discover_active_window` / `get_book` / `best_bid_ask` against `https://api-testnet.predict.fun` (no key); sync props (`name="predict_fun"`, `is_testnet=True`, `collateral="USDT"`, `chain_id=97`); `live_disabled_reason()` reading `PREDICT_LIVE`/`PREDICT_PRIVATE_KEY`; order/portfolio methods raise `NotImplementedError` (guarded).

- [ ] **Step 1: Write the failing test** (mock the REST responses; assert **Up/Down mapped by NAME**, and that reversed outcome order does NOT invert direction)

```python
# engine/tests/test_predict_fun_venue.py
from __future__ import annotations
from unittest.mock import AsyncMock, patch
import pytest
from venues.predict_fun import PredictFunVenue

# a market object shaped like GET /v1/markets?marketVariant=CRYPTO_UP_DOWN (outcomes REVERSED on purpose)
_MARKET = {
    "id": 778011, "conditionId": "0x40c806", "categorySlug": "btc-updown-5m-1700000100",
    "status": "OPEN", "tradingStatus": "OPEN", "feeRateBps": 200,
    "outcomes": [
        {"indexSet": 2, "name": "Down", "onChainId": "24946845", "bestBid": 0.12, "bestAsk": 0.13},
        {"indexSet": 1, "name": "Up",   "onChainId": "45899948", "bestBid": 0.87, "bestAsk": 0.88},
    ],
}


def test_identity_props():
    v = PredictFunVenue()
    assert (v.name, v.is_testnet, v.collateral, v.chain_id) == ("predict_fun", True, "USDT", 97)


@pytest.mark.asyncio
async def test_discover_maps_up_down_by_name_not_index():
    v = PredictFunVenue()
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[_MARKET])):
        m = await v.discover_active_window("5m")
    assert m is not None
    assert m.token_up == "45899948"     # the outcome NAMED "Up", despite being 2nd in the array
    assert m.token_down == "24946845"
    assert m.window_sec == 300
    assert m.condition_id == "0x40c806"


@pytest.mark.asyncio
async def test_order_methods_raise_notimplemented_in_m2a():
    v = PredictFunVenue()
    with pytest.raises(NotImplementedError):
        await v.place_entry_order("T", 10, 0.5, "Up")
    with pytest.raises(NotImplementedError):
        await v.place_exit_order("T", 10, 0.5)


def test_live_disabled_reason_reads_predict_env(monkeypatch):
    v = PredictFunVenue()
    monkeypatch.delenv("PREDICT_LIVE", raising=False)
    assert v.live_disabled_reason() is not None            # no PREDICT_LIVE => disabled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest engine/tests/test_predict_fun_venue.py -v` → FAIL (methods unimplemented).

- [ ] **Step 3: Fill `engine/venues/predict_fun.py`** (read-only; REST via httpx; NO SDK needed):

```python
# engine/venues/predict_fun.py
"""PredictFunVenue — Predict.fun (BNB Chain) venue. M2a = READ-ONLY on testnet (discover + book).
Order placement raises until M2b. Testnet base has no API key; respect 240 req/min."""
from __future__ import annotations
import os
from typing import Any, Optional

import httpx
from .base import Venue, ActiveMarket

_TESTNET_BASE = "https://api-testnet.predict.fun"


def _is_testnet() -> bool:
    # default-safe: only explicit opt-out points at mainnet
    return os.environ.get("PREDICT_MAINNET", "").strip() != "1"


class PredictFunVenue(Venue):
    name = "predict_fun"

    def __init__(self) -> None:
        self._testnet = _is_testnet()

    @property
    def is_testnet(self) -> bool: return self._testnet
    collateral = "USDT"
    @property
    def chain_id(self) -> int: return 97 if self._testnet else 56
    @property
    def _base(self) -> str: return _TESTNET_BASE  # mainnet base wired in M3

    async def _get_open_crypto_updown_markets(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{self._base}/v1/markets",
                            params={"marketVariant": "CRYPTO_UP_DOWN", "status": "OPEN"})
            r.raise_for_status()
            data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def discover_active_window(self, window: str) -> Optional[ActiveMarket]:
        want_sec = 900 if window == "15m" else 300
        prefix = "btc-updown-15m-" if window == "15m" else "btc-updown-5m-"
        markets = await self._get_open_crypto_updown_markets()
        for mk in markets:
            slug = str(mk.get("categorySlug", ""))
            if not slug.startswith(prefix):
                continue
            outs = {str(o.get("name")).lower(): o for o in mk.get("outcomes", [])}
            up, down = outs.get("up"), outs.get("down")
            if not up or not down:
                continue
            try:
                epoch = int(slug.rsplit("-", 1)[-1])
            except ValueError:
                continue
            up_bid, up_ask = up.get("bestBid"), up.get("bestAsk")
            down_bid = down.get("bestBid")
            return ActiveMarket(
                slug=slug, epoch=epoch, condition_id=str(mk.get("conditionId", "")),
                end_date_iso=str(mk.get("endDate", "") or mk.get("boostEndsAt", "")),
                closed=(str(mk.get("tradingStatus", "OPEN")).upper() != "OPEN"),
                token_up=str(up.get("onChainId")), token_down=str(down.get("onChainId")),
                outcome_prices=(float(up_ask) if up_ask is not None else 0.0,
                                float(down_bid) if down_bid is not None else 0.0),
                order_min_size=float(mk.get("minOrderSize", 1) or 1),
                title=str(mk.get("question", mk.get("title", ""))),
                window_sec=want_sec, order_min_size_source="clob",
                resolution_source="predict.fun CRYPTO_UP_DOWN",
            )
        return None

    async def get_book(self, client: Any, token_id: str) -> dict:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{self._base}/v1/markets/{token_id}/orderbook")
            r.raise_for_status()
            book = r.json()
        return book.get("data", book) if isinstance(book, dict) else book

    async def best_bid_ask(self, token_id: str):
        # M2a: derive top-of-book from the market object's inlined bestBid/bestAsk (single Up-based book).
        markets = await self._get_open_crypto_updown_markets()
        for mk in markets:
            for o in mk.get("outcomes", []):
                if str(o.get("onChainId")) == str(token_id):
                    bid, ask = o.get("bestBid"), o.get("bestAsk")
                    return (float(bid) if bid is not None else None,
                            float(ask) if ask is not None else None)
        return (None, None)

    # --- orders: NOT in M2a (M2b) ---
    def _order_guard(self):
        raise NotImplementedError("PredictFunVenue order placement is M2b (testnet-first, behind the triple lock).")
    async def place_entry_order(self, *a, **k): self._order_guard()
    async def place_exit_order(self, *a, **k): self._order_guard()
    async def fetch_portfolio(self, *, force: bool = False) -> dict:
        return {"ok": False, "error": "predict_fun portfolio is M2b", "balance_usd": 0.0,
                "positions": [], "equity_usd": 0.0, "address": None, "funder_address": None,
                "is_proxy": False, "hint": "testnet trading not yet enabled"}
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
```
> The `best_bid_ask`/`get_book` token semantics (single Up-based book; Down = 1−price) and the exact market JSON keys must be confirmed against a REAL testnet response — see `m2-research-market-map.md`. If a field name differs (e.g. `minOrderSize`), adjust to the real key; the TEST mocks the response, so also update the mock to the real shape once confirmed.

- [ ] **Step 4: Run tests + a real testnet smoke check**

Run: `python3 -m pytest engine/tests/test_predict_fun_venue.py -v` → PASS. Then full suite `-q`.
Live read-only smoke (network; confirms we're hitting the real testnet book): a tiny script that calls `await PredictFunVenue().discover_active_window("5m")` and prints the discovered `slug/epoch/token_up/token_down/condition_id`. **Record the result** — this is the "same book?" evidence gate (compare `conditionId`/`onChainId` shape vs the Binance-Wallet product; flag CONFIRMED/UNVERIFIED per spec §9). If the network is unavailable in the sandbox, note it and leave the smoke for the controller.

- [ ] **Step 5: Commit**

```bash
git add engine/venues/predict_fun.py engine/tests/test_predict_fun_venue.py
git commit -m "feat(venues): PredictFunVenue read-only on BNB testnet (discover+book; orders raise)"
```

---

## Self-Review notes
- **Spec coverage:** M2a items from the spec §6 — Venue interface + registry (Task 1), PolymarketVenue pure delegation + runner reroute with byte-identical verification (Task 2), order_venue field + endpoint (Task 3), PredictFunVenue read-only + label→tokenId-by-name + same-book gate (Task 4). Order placement, triple-lock wiring, mismatch guard, UI = M2b/M3 (out of scope).
- **Type/async consistency:** async vs sync per method is fixed in `base.Venue` and asserted in Task 1's `test_venue_async_sync_shape`; Task 2 preserves `await` at every rerouted site.
- **Highest risk = Task 2** (rerouting the real-money order path). Its gate is the full-suite green + grep-clean + a manual byte-identical check; the reviewer must confirm no strategy-math line changed and every `await` was preserved.
- **Known unknowns (verify during build, per spec §9):** exact Predict.fun testnet market JSON keys (`minOrderSize`, orderbook shape), and the same-book question (Task 4 smoke gates it). Mocks encode the researched shape; adjust to the real response.
