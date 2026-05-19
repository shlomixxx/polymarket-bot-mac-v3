# Duplicate API Requests Audit

**Scope:** Full codebase (frontend `src/` + backend `engine/`) inspected for redundant external/internal HTTP requests that produce the **same result** more than once within overlapping windows.

**Method:** Static review of fetch call sites, polling intervals, cache TTLs, and internal function call graphs. No code was modified.

**Legend:**
- **External duplicate** — repeated hit to a third‑party API (Polymarket Gamma/CLOB, Binance, Chainlink, Alternative.me) returning the same data.
- **Internal duplicate** — repeated hit to the bot's own FastAPI engine returning the same cached/derived state.
- **Cache bypass** — a cache exists but the call path skips it.

---

## Section 1 — Frontend ↔ Frontend Overlap

When multiple panels/layouts are mounted at the same time (the default live-stream dashboards mount `App` alongside `LiveStreamTrade` / `TriggerTrader` / `SignalsPanel`), the browser sends the same request from each mount on its own timer. The responses are identical; only one is really needed.

### 1.1 `/api/demo/snapshot` polled twice (every ~500 ms)
Both places poll the same endpoint at the same cadence.
- [src/App.tsx:1966-1989](src/App.tsx#L1966-L1989) — 500 ms interval
- [src/LiveStreamTrade.tsx:863-890](src/LiveStreamTrade.tsx#L863-L890) — 500 ms interval

➡ Net effect: ~2 snapshot requests per 500 ms (~240/min) when the broadcast layout is open alongside the main dashboard.

### 1.2 `/api/live/portfolio` polled twice
- [src/App.tsx:1992-2013](src/App.tsx#L1992-L2013) — every 3500 ms while live mode is on
- [src/LiveStreamTrade.tsx:914-931](src/LiveStreamTrade.tsx#L914-L931) — every 5000 ms

Same payload, two consumers.

### 1.3 `/api/trigger/state` polled three ways
- [src/LiveStreamTrade.tsx:898-911](src/LiveStreamTrade.tsx#L898-L911) — every 2000 ms
- [src/TriggerTrader.tsx:329-353](src/TriggerTrader.tsx#L329-L353) — every 2000 ms
- [src/App.tsx:1799-1936](src/App.tsx#L1799-L1936) — pulled inside the batched `refresh()` every 1500-3000 ms

All three read the same in-memory trigger struct.

### 1.4 `refresh()` fan-out duplicates between App and LiveStreamTrade
Each component's `refresh()` fires a `Promise.all` bundle. The bundles overlap:

| Endpoint | App.tsx refresh() | LiveStreamTrade refresh() |
|----------|---------------------------|------------------------------------|
| `/api/config` | ✓ | ✓ |
| `/api/market/current` | ✓ | ✓ |
| `/api/btc/live` | ✓ | ✓ |
| `/api/market/orderbook-summary` | ✓ | ✓ |
| `/api/signals` | ✓ (plus SignalsPanel) | ✓ |
| `/api/demo/state` | ✓ | ✓ |
| `/api/trigger/state` | ✓ | ✓ |

- [src/App.tsx:1799-1936](src/App.tsx#L1799-L1936) + loop [src/App.tsx:1949-1963](src/App.tsx#L1949-L1963)
- [src/LiveStreamTrade.tsx:730-760](src/LiveStreamTrade.tsx#L730-L760) + loop [src/LiveStreamTrade.tsx:853-860](src/LiveStreamTrade.tsx#L853-L860)

➡ Every ~2 s both components independently refetch the same 5-7 endpoints.

### 1.5 `/api/signals` polled by App **and** SignalsPanel
- [src/App.tsx:1799-1936](src/App.tsx#L1799-L1936) (inside batched refresh)
- [src/SignalsPanel.tsx:444-450](src/SignalsPanel.tsx#L444-L450) — every 5000 ms

Signals are the same computation for both.

### 1.6 `/api/contract-prices` polled by SignalsPanel while App pulls the same bid/ask via WS + orderbook-summary
- [src/SignalsPanel.tsx:453-476](src/SignalsPanel.tsx#L453-L476) — every 750 ms
- [src/App.tsx:1674-1688](src/App.tsx#L1674-L1688) — WS price stream updates orderbook state in App
- [src/App.tsx:1799-1936](src/App.tsx#L1799-L1936) — also pulls `/api/market/orderbook-summary` on each refresh

The three sources all surface UP/DOWN best bid/ask. Either the WebSocket stream or `/api/contract-prices` would suffice on its own.

### 1.7 WebSocket price stream + REST orderbook redundancy
`usePriceStream()` subscribes to `/ws/prices` and produces live bid/ask. The same numbers are re-delivered by the `/api/market/orderbook-summary` polled inside `refresh()`.
- [src/App.tsx:1672](src/App.tsx#L1672)
- [src/App.tsx:1674-1688](src/App.tsx#L1674-L1688)
- [src/App.tsx:1799-1936](src/App.tsx#L1799-L1936)

➡ Not strictly duplicate (REST carries more fields), but the top-of-book figures are fetched twice.

---

## Section 2 — Backend Endpoint Handlers Hitting the Same Upstream

Multiple FastAPI handlers independently recompute identical upstream data per invocation. Because frontend polls them every 1-3 s, the amplification is significant.

### 2.1 `discover_active_btc_window` re-called by every market handler
`market_discovery.discover_active_btc_window` makes **1-20+** Gamma API calls per invocation and is **not cached** at the discovery layer.
- [engine/market_discovery.py:123-151](engine/market_discovery.py#L123-L151)

Called from at least:
- [engine/main.py:638-680](engine/main.py#L638-L680) (`/api/market/current`)
- [engine/main.py:693-701](engine/main.py#L693-L701) (`/api/btc/window-prices`)
- [engine/main.py:730-807](engine/main.py#L730-L807) (`/api/market/orderbook-summary`)
- [engine/main.py:1398-1443](engine/main.py#L1398-L1443) (`/api/signals`)
- [engine/main.py:1453-1514](engine/main.py#L1453-L1514) (`/api/contract-prices`)
- [engine/main.py:1689](engine/main.py#L1689) (`/api/trigger/share-bundle`)
- [engine/strategy_runner.py:770](engine/strategy_runner.py#L770) — strategy `_tick` (~1 Hz)
- [engine/trigger_engine.py:264-279](engine/trigger_engine.py#L264-L279) (`_fetch_contract_ask`)
- [engine/trigger_engine.py:281-290](engine/trigger_engine.py#L281-L290) (`_get_window_info`)
- [engine/trigger_engine.py:499-535](engine/trigger_engine.py#L499-L535) (`_resolve_dca_side`, ~3 s loop)
- [engine/trigger_engine.py:537](engine/trigger_engine.py#L537) (`_auto_wait_for_best_price`, ~2 s loop)
- [engine/trigger_engine.py:953-957](engine/trigger_engine.py#L953-L957) (post-TP sell_ctx)

➡ Within any 1 s window the same window lookup is performed 5-8 times at minimum. `cached_open` caches the *open price*, not the window discovery.

### 2.2 `get_clob_book(token_id)` — repeated per request cycle
Each of the following handlers calls `get_clob_book` twice (UP + DOWN) on every invocation:
- [engine/main.py:730-807](engine/main.py#L730-L807) (`/api/market/orderbook-summary`, 0.5 s cache)
- [engine/main.py:1398-1443](engine/main.py#L1398-L1443) (`/api/signals`)
- [engine/main.py:1453-1514](engine/main.py#L1453-L1514) (`/api/contract-prices`, 0.5 s cache)
- [engine/main.py:1689](engine/main.py#L1689) (`/api/trigger/share-bundle`)

WebSocket cache (`ws_price_stream.price_stream.get_price`) is checked first in some paths, but `/api/signals` and `/api/trigger/share-bundle` fall through to REST on every call. Combined with Section 2.1 callers, the CLOB `/book` endpoint is queried many times per second for the same two token IDs.
- [engine/market_discovery.py:164-183](engine/market_discovery.py#L164-L183) (`get_clob_book`)

### 2.3 `fetch_polymarket_clob_account` hit twice per cycle
Two separate endpoints independently refetch the CLOB balance:
- [engine/main.py](engine/main.py) — `/api/live/polymarket-clob-account` handler calls [engine/live_clob.py:639-697](engine/live_clob.py#L639-L697) directly (no cache)
- `/api/live/portfolio` handler calls [engine/live_clob.py:789-854](engine/live_clob.py#L789-L854) `fetch_live_portfolio`, which **internally** also calls `fetch_polymarket_clob_account`

`fetch_live_portfolio` has a 2 s result cache, but `fetch_polymarket_clob_account` does **not** — so even intra-handler, the balance fetch is not reused. When the frontend polls both portfolio and the raw CLOB account (it does, via App.tsx refresh + live/portfolio loop), each refresh round results in at least **two** CLOB balance HTTP calls.

### 2.4 `/api/btc/live` uncached spot fetch
- [engine/main.py:683-690](engine/main.py#L683-L690) calls `fetch_btc_spot_usdt` with no caching.

The same Binance price is re-requested at frontend cadence (~2-3 s from App.tsx + LiveStreamTrade). With both mounted, Binance sees the doubled rate.

### 2.5 `/api/btc/window-prices` double upstream
- [engine/main.py:693-701](engine/main.py#L693-L701) makes 2 Binance klines calls per invocation (open + close). The open price is *also* fetched by `fetch_open_price_at_window_start` used elsewhere in the engine — the two paths don't share a cache.

---

## Section 3 — Engine-Loop Internal Duplicates

Inside the long‑running loops, the same upstream is pulled multiple times per tick.

### 3.1 `strategy_runner._tick` — up/down ask fetched twice
Per tick the runner calls `fetch_best_bid_ask(token_up)` and `fetch_best_bid_ask(token_down)`, then re-iterates open positions calling `fetch_best_bid_ask(p.token_id)`. Because an open position's `token_id` is almost always one of `token_up`/`token_down`, the same best bid/ask is fetched a second time within microseconds.
- [engine/strategy_runner.py:308-324](engine/strategy_runner.py#L308-L324) (`fetch_best_bid_ask`)
- [engine/strategy_runner.py:744](engine/strategy_runner.py#L744) (`_tick` loop)

### 3.2 `trigger_engine._validate_contract_entry` — double window discovery
The validator calls `_get_window_info()` **and** `_fetch_contract_ask()`, and each of them independently calls `discover_active_btc_window`.
- [engine/trigger_engine.py:264-279](engine/trigger_engine.py#L264-L279) (`_fetch_contract_ask`)
- [engine/trigger_engine.py:281-290](engine/trigger_engine.py#L281-L290) (`_get_window_info`)
- [engine/trigger_engine.py:314-353](engine/trigger_engine.py#L314-L353) (`_validate_contract_entry`)

➡ 2× `discover_active_btc_window` per validation, plus one CLOB `/book` fetch.

### 3.3 `trigger_engine._resolve_dca_side` loop — redundant with `_auto_wait_for_best_price`
Both helpers independently call `discover_active_btc_window + get_clob_book(UP) + get_clob_book(DOWN)` every ~2-3 s while a trigger is armed.
- [engine/trigger_engine.py:499-535](engine/trigger_engine.py#L499-L535) (`_resolve_dca_side`, 3 s loop)
- [engine/trigger_engine.py:537](engine/trigger_engine.py#L537) (`_auto_wait_for_best_price`, ~2 s loop)

If both run concurrently (arming → DCA path), the same book is fetched by two loops.

### 3.4 `trigger_engine._check_tp_exits` — per-position `/book`
Iterates positions and calls `get_clob_book(position.token_id)` for each. When all positions share the same `token_id` (same contract), the book is fetched once per position instead of once total.
- [engine/trigger_engine.py:894-980](engine/trigger_engine.py#L894-L980)
- After a TP fill, [engine/trigger_engine.py:953-957](engine/trigger_engine.py#L953-L957) calls `discover_active_btc_window` again even though the same function was called at the top of the tick.

### 3.5 `demo_engine.best_ask` duplicates `fetch_best_bid_ask`
`demo_engine.best_ask` does its own raw `/book` fetch rather than reading the WS-backed cache used by `strategy_runner.fetch_best_bid_ask`.
- [engine/demo_engine.py:605-622](engine/demo_engine.py#L605-L622) (`best_ask`)
- [engine/demo_engine.py:624-716](engine/demo_engine.py#L624-L716) (`simulate_market_buy` — calls `best_ask` just after strategy_runner already fetched it)

➡ Two `/book` round‑trips within the same decision window.

### 3.6 `demo_engine.reset_stats_and_flatten_positions` — per-position `/book`
- [engine/demo_engine.py:230-271](engine/demo_engine.py#L230-L271)

Each open position triggers a separate `/book` fetch even when all share the same token.

### 3.7 `demo_engine.simulate_sell_all` — per-position `/book`
- [engine/demo_engine.py:986-1066](engine/demo_engine.py#L986-L1066)

Same pattern as 3.6.

### 3.8 `demo_engine.mark_to_market` — per-position and per post-exit token
- [engine/demo_engine.py:1068-1325](engine/demo_engine.py#L1068-L1325)
- Post-exit tracking: [engine/demo_engine.py:1220](engine/demo_engine.py#L1220) issues another `/book` call per tracked token.

WS cache is preferred, but on miss each position falls back to an independent REST call even when multiple positions share a token.

---

## Section 4 — Cache Bypass Issues

### 4.1 `/api/signals` bypasses the 30 s signal cache
`signal_engine._signals_cache` only returns a hit when `up_book is None and down_book is None`.
- [engine/signal_engine.py:28-30](engine/signal_engine.py#L28-L30) (cache declaration)
- [engine/signal_engine.py:66-74](engine/signal_engine.py#L66-L74) (cache guard)

The `/api/signals` handler always passes `up_book` and `down_book` (so the values match the current CLOB), which means the cache **never** hits — every call re-computes `compute_signals`, which in turn calls `fetch_btc_klines` (Binance) and may touch funding/fear_greed.
- [engine/main.py:1398-1443](engine/main.py#L1398-L1443) (`/api/signals`)

➡ Binance klines fetched on every `/api/signals` poll (every ~2-5 s from two frontends).

### 4.2 `ta_signals.fetch_btc_klines` uncached at the source
- [engine/ta_signals.py:15-35](engine/ta_signals.py#L15-L35)

Caching is only provided by the upstream signal cache, which (per 4.1) is bypassed.

### 4.3 `fetch_polymarket_clob_account` not cached
Called twice per cycle (see 2.3). A cache of even 1-2 s would halve the CLOB REST pressure.
- [engine/live_clob.py:639-697](engine/live_clob.py#L639-L697)

### 4.4 `fetch_btc_spot_usdt` not cached
Called from `/api/btc/live` on every frontend poll, plus internal callers.
- [engine/main.py:683-690](engine/main.py#L683-L690)

### 4.5 Partial caches that re-fetch internally
- `/api/market/orderbook-summary` → 0.5 s cache on the *endpoint* response, but internal callers of `get_clob_book` (e.g. `/api/signals`, trigger loops) don't share that cache.
- `/api/contract-prices` → 0.5 s cache, same issue.
- `/api/live/portfolio` → 2 s cache, but internal `fetch_polymarket_clob_account` is not cached.

➡ The per-endpoint micro-caches protect the handler response but not the **upstream** calls shared with other paths.

---

## Section 5 — Missing or Weak Caches (Summary)

| Call | Where | Suggested cache window |
|------|-------|------------------------|
| `discover_active_btc_window` | [engine/market_discovery.py:123](engine/market_discovery.py#L123) | 5-15 s (epoch boundary aware) |
| `get_clob_book(token_id)` shared | [engine/market_discovery.py:164](engine/market_discovery.py#L164) | unify with existing 0.5 s cache so **all** callers share it |
| `fetch_btc_spot_usdt` | [engine/btc_price.py](engine/btc_price.py) | 0.5-1 s |
| `fetch_btc_klines` | [engine/ta_signals.py:15](engine/ta_signals.py#L15) | 15-30 s |
| `fetch_polymarket_clob_account` | [engine/live_clob.py:639](engine/live_clob.py#L639) | 1-2 s |
| `fetch_open_price_at_window_start` / `fetch_close_price_at_window_end` | [engine/btc_price.py](engine/btc_price.py) | until window rollover |

---

## Section 6 — Duplicate Request Hotspots (Ranked by Traffic Impact)

Approximate rate when the default broadcast layout + main dashboard are open and the trigger engine is armed:

1. **`discover_active_btc_window` → Gamma API** — fired ~8-12×/s aggregated across handlers + loops. 1-20 Gamma calls per invocation = dozens of Gamma requests per second for the same answer. *(Sections 2.1, 3.2, 3.3)*
2. **`get_clob_book(token_up|token_down)` → CLOB `/book`** — fired ~6-10×/s for each of the two tokens. *(Sections 2.2, 3.1, 3.3, 3.4, 3.5-3.8)*
3. **`/api/signals` → Binance klines + sentiment** — fired every poll due to cache bypass. *(Section 4.1)*
4. **Frontend `refresh()` overlap** — App + LiveStreamTrade double every endpoint in the bundle every ~2 s. *(Sections 1.1-1.5)*
5. **CLOB balance** — fetched twice per refresh cycle. *(Section 2.3)*
6. **`fetch_btc_spot_usdt`** — doubled by frontend overlap and again by internal callers. *(Sections 1.4, 2.4)*

---

## Appendix A — File / Symbol Index

Frontend:
- [src/api.ts](src/api.ts) — central fetch wrapper
- [src/App.tsx](src/App.tsx)
- [src/LiveStreamTrade.tsx](src/LiveStreamTrade.tsx)
- [src/TriggerTrader.tsx](src/TriggerTrader.tsx)
- [src/SignalsPanel.tsx](src/SignalsPanel.tsx)
- [src/hooks/usePriceStream.ts](src/hooks/usePriceStream.ts)

Backend:
- [engine/main.py](engine/main.py)
- [engine/market_discovery.py](engine/market_discovery.py)
- [engine/strategy_runner.py](engine/strategy_runner.py)
- [engine/trigger_engine.py](engine/trigger_engine.py)
- [engine/demo_engine.py](engine/demo_engine.py)
- [engine/signal_engine.py](engine/signal_engine.py)
- [engine/ta_signals.py](engine/ta_signals.py)
- [engine/sentiment.py](engine/sentiment.py)
- [engine/live_clob.py](engine/live_clob.py)
- [engine/btc_price.py](engine/btc_price.py)
- [engine/ws_price_stream.py](engine/ws_price_stream.py)

---

**Note:** This report is audit-only. No code was modified. Line numbers reflect the state of `main` at audit time (commit `7015ce4`); if files are edited afterward the anchors may drift.
