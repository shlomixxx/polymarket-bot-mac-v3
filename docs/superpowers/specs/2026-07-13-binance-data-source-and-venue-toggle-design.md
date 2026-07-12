# Design: Polymarket ⟷ Binance data-source & venue toggle

- **Date:** 2026-07-13
- **Status:** Draft — awaiting owner review
- **Branch context:** currently on `fix/martingale-blowup-guard`
- **Related:** [[binance-bot-and-learning-advisor]], [[chainlink-price-feed]], [[polymarket-compliance]], [[martingale-incident-and-faults-tab]]

---

## תקציר לבעלים (Hebrew executive summary)

המטרה שלך: כפתור אחד בלוח הבקרה — **`Polymarket ⟷ Binance`** — ששולט גם **מאיפה המערכת קוראת את נתוני ה-BTC** וגם **לאן נשלחות הפקודות**. אותה מערכת, אותה אסטרטגיה, רק להחליף בורסה בלחיצה, ולדעת תמיד בוודאות איפה אתה סוחר.

בונים בשני שלבים (לפי בחירתך — קודם נתונים, אחר כך פקודות):

- **שלב 1 — מקור הנתונים.** מתג שמעביר את *כל* צינור הנתונים (מחיר נוכחי, "מחיר לנצח", הכרעת ניצחון/הפסד, וכל הסטטיסטיקה והדמו) לקרוא מ-Binance במקום מ-Chainlink של Polymarket. רוב הקוד כבר קיים. אין צורך בארנק, אין סיכון כסף אמיתי. זה גם מתקן אי-עקביות קיימת (היום הדמו כבר מכריע לפי Binance בעוד החי לפי Chainlink).
- **שלב 2 — שליחת פקודות ל-Binance up/down.** "Binance up/down" האמיתי הוא **Predict.fun** (השותף של Binance, על הבלוקצ'יין BNB). מוסיפים "מתאם בורסה" שמנתב פקודות לשם. מתחילים ב**טסטנט** (כסף מזויף); כסף אמיתי רק מאחורי אותם שלושה מנעולי-בטיחות כמו Polymarket.

**אמת שחייבים לומר:** זו אותה אסטרטגיה בלי יתרון מוכח, ועמלת ~2% ב-Predict.fun אוכלת את הקצה — בדיוק כמו ה-~7-8% ב-Polymarket. הכפתור *לא* יוצר רווחיות; הוא רק נותן לך בחירת בורסה ובהירות. לכן שלב 2 מתחיל בטסטנט וכסף אמיתי כבוי כברירת מחדל.

---

## 1. Goals & non-goals

### Goals
1. A single, always-visible **venue selector** (`Polymarket ⟷ Binance`) in the control panel that unambiguously shows, at all times: **which venue** is active, whether it is **testnet/demo or real money**, and **where BTC data is read from**.
2. **Phase 1:** switch the entire BTC data pipeline (current price, price-to-beat, win/loss settlement, and therefore all demo stats) between Polymarket-Chainlink and Binance.
3. **Phase 2:** route real orders to the Binance up/down market (Predict.fun on BNB Chain), testnet-first, behind the same triple safety lock as Polymarket live trading.
4. Preserve the existing strategy logic unchanged — only *where data comes from* and *where orders go* changes.

### Non-goals
- Creating or claiming trading edge. This design does not change EV. The standing verdict holds: no proven edge on short-window BTC; fees are punishing.
- Leverage / futures. Predict.fun up/down is fixed-payout (like Polymarket), **no leverage** — this is deliberately *not* the Binance Futures cockpit path (`engine/binance_cockpit.py`), which stays a separate manual tool.
- A second Railway service. This is one dashboard, one engine, one selector (rejected "separate service" approach C).

---

## 2. Research findings that shape the design (verified 2026-07-13)

### 2.1 "Binance up/down" is Predict.fun
Binance's own CEX has **no** binary fixed-payout up/down product (its `/eapi` options are variable-payout vanilla options; the old American daily options were delisted 2022-12). The genuine up/down market — surfaced in Binance Wallet as "Event Rush" since April 2026 — is **Predict.fun**, an on-chain Polymarket-style CLOB on **BNB Chain**. It has live **5-minute and 15-minute BTC Up/Down** markets (`marketVariant=CRYPTO_UP_DOWN`), EIP-712 signed orders, official Python (`predict-sdk`) + TS SDKs, and a **no-API-key public testnet** (BNB testnet, chain 97). **Israel is not geo-restricted** in the Predict.fun ToS (US/UK/FR/etc. are). Sources captured in the 2026-07-13 research agents.

> Open item to verify during build: one source describes "Event Rush" as a bonding-curve product on a separate protocol. The Phase-2 read-only step must confirm the API CLOB is the same book Binance Wallet users trade before we scale up.

### 2.2 The two venues use *different* oracles
| | Polymarket | Predict.fun |
|---|---|---|
| Settlement price | Chainlink **Data Streams** BTC/USD (multi-CEX aggregate), on Polygon, direct settle ~2–5 min, no dispute path | Chainlink **DataLink** over **Binance order-book** data, on BNB Chain, with a **UMA optimistic-oracle** backstop (disputes can delay/alter payout by days) |
| Taker fee | 0% nominal; ~7–8% effective spread/vig | ~2% (`feeRateBps: 200`, read live per market) |

They usually agree on direction but can **diverge on near-boundary windows** (Binance-only vs multi-CEX aggregate, snapshot timing, rounding), and Predict.fun carries genuine **dispute/settlement-delay** risk.

### 2.3 Our engine already reads Binance — and there's a latent inconsistency to fix
- Live decision price: `engine/btc_price.py::fetch_btc_current_usd` (L238-254) prefers the Chainlink stream, **falls back to Binance spot**.
- Window open/close: `fetch_open_price_at_window_start` (L257), `fetch_close_price_at_window_end` (L279), `fetch_window_start_end_btc_usd` (L340) all read **Binance 1m klines** and return `source:"binance_1m_proxy"`.
- **Demo win/loss is already scored off Binance** (`engine/demo_engine.py` L1310-1329), while live trading reads Chainlink (`engine/chainlink_price_stream.py`). Near the boundary these can disagree → a demo "win" can be a real "loss".
- Polymarket share bid/ask: `engine/ws_price_stream.py` (CLOB WS). Window timing + token discovery: `engine/market_discovery.py` (Gamma API).

**Implication:** Phase 1 is genuinely light because Binance price code exists. The data-source toggle also *cleans up* the demo-vs-live settlement inconsistency by making the whole pipeline read one source consistently.

### 2.4 No venue abstraction exists today
Polymarket is hardcoded via `engine/live_clob.py`, called directly from `engine/strategy_runner.py` (order placement at ~L648, L804-807, L895-903; live gate `_live_trading_ok` L617). Phase 2 introduces the seam.

---

## 3. Data model

Two internal fields, surfaced to the user as **one button** whose two positions are `polymarket` and `binance`:

| Field | Phase | Values | Default | Persistence |
|---|---|---|---|---|
| `data_source` | 1 | `"polymarket"` \| `"binance"` | `"polymarket"` | persisted (survives restart) |
| `order_venue` | 2 | `"polymarket"` \| `"predict_fun"` | `"polymarket"` | persisted; **real-money gated** |

- The header button sets **both** in lockstep for the user's mental model ("I'm on Binance"). Phase 1 ships with only `data_source` wired; the button in Phase 1 is scoped/labeled to data, with a small sub-label "פקודות: Polymarket (בקרוב Binance)". Phase 2 unifies them.
- Follows the existing persisted-toggle pattern (`live_trading`): dedicated `GET/POST /api/venue` endpoint, written to `engine/config_persisted.json` via `_save_persisted_config`, restored on load. Add the field in the five standard places (StrategyConfig dataclass, `ConfigBody`, POST validation, persist dict, GET response) per the config map.
- Like `mode`, we may choose **not** to auto-restore `order_venue=predict_fun` into a live state on restart — real trading must be re-armed deliberately (mirrors "mode resets to off").

---

## 4. Phase 1 — data-source toggle (build first)

### 4.1 Engine wiring
Introduce a single source-of-truth resolver the price functions consult, e.g. `engine/data_source.py::active_source()` reading `runner.rt.config.data_source`. Route the existing functions by it:

- `btc_price.fetch_btc_current_usd`: when `binance`, read Binance spot directly (reuse `fetch_btc_spot_usdt`) and label `source="binance"`; when `polymarket`, keep Chainlink-stream-preferred behavior.
- Price-to-beat / window open-close: when `binance`, use the Binance-kline functions consistently for **both** the displayed PTB and the settlement; when `polymarket`, use the Chainlink Data Stream PTB (`chainlink_stream.get_price_to_beat`).
- `/api/market/current` (`engine/main.py` L1144-1186) and `/api/btc/live` (L1208-1238): honor `data_source` instead of always preferring Chainlink.
- **Settlement consistency fix:** demo win/loss (`demo_engine.py` L1310-1329) and (later) live resolution both read the *selected* source, removing the current demo=Binance / live=Chainlink split. Each recorded trade is tagged with the `data_source` it was settled on.

### 4.2 Stats / demo
**The existing statistics surfaces are reused unchanged** — same tabs (`stats`, `stats_live`, demo history), same layout, same metrics the owner has today. We do **not** build a new stats system. Only the *underlying data source* switches: because stats derive from recorded trades + their settlement, once settlement reads the selected source and each trade is tagged with its `data_source`, "same statistics as today, but based on Binance" follows automatically. Verify the stats/audit read paths surface the tag (e.g. history rows, `stats`/`stats_live` tabs) so a Binance-based number is never mistaken for a Polymarket one.

### 4.3 UI (Phase 1)
- A prominent toggle in the dashboard header, always visible: **מקור נתונים: Polymarket / Binance**, distinct color + icon per side.
- A badge on the stats, demo, and dashboard views showing which source the numbers reflect (so a Binance-based stat is never mistaken for a Polymarket one).
- Persists through the new `/api/venue` endpoint (or the existing `pushConfig` path).

### 4.4 Phase 1 risks handled
- Stale-feed / cold-start: document that Binance spot (HTTP, 1s cache, 30s stale fallback) replaces the Chainlink snapshot mechanics; keep the existing freshness guards.
- No wallet, no real money, no order-path change → Phase 1 is safe to ship independently.

---

## 5. Phase 2 — order routing to Predict.fun (build second)

### 5.1 Venue abstraction seam
Introduce a thin `Venue` interface (`engine/venues/base.py`) with the methods the runner needs: `discover_markets`, `get_book/price`, `place_entry_order`, `place_exit_order`, `fetch_positions`, `fetch_portfolio`, `redeem`. Two implementations:
- `PolymarketVenue` — wraps existing `live_clob.py` unchanged.
- `PredictFunVenue` — new: REST (`/v1/markets`, `/orderbook`, `/orders`, `/positions`), JWT auth (wallet signature), EIP-712 order signing via `predict-sdk`, BNB-chain gas for approvals/redemptions, USDT collateral, `feeRateBps` read live per market, `isNegRisk`/`isYieldBearing` handling, and **UMA dispute-window** awareness (a position may resolve/pay out after a delay).

`strategy_runner` calls `self.venue.*` instead of `live_clob.*` at the ~4 call sites. Strategy logic is untouched.

### 5.2 Testnet-first
Default `PredictFunVenue` to BNB testnet (chain 97, no API key). Prove end-to-end on testnet (discover → place → fill → settle/redeem) before any mainnet path. Mainnet requires a Discord-issued API key + real USDT on BNB.

### 5.3 Safety model (real money)
Real trading on **either** venue stays behind the **triple lock**, per venue:
1. Env kill-switch (`PREDICT_LIVE=1`, mirroring `POLYMARKET_LIVE`) — absent ⇒ testnet/demo only.
2. Explicit in-app "real money" toggle.
3. Wallet key present.

Plus: per-venue loss caps / equity floor; the advisor/UI **never claims profitable** while net EV ≤ 0; real money **off by default**; the button loudly shows **testnet vs REAL MONEY** (distinct red state). Martingale (chop-armed FLW) on real money carries the documented blow-up risk (see `fix/martingale-blowup-guard`); it stays under the existing guard and is surfaced, not silently combined with a new venue.

### 5.4 Unified button
In Phase 2 the header button's two positions (`Polymarket` / `Binance`) set both `data_source` and `order_venue` together. The badge shows: venue · data source · testnet-or-real · (for Predict.fun) "הכרעה עשויה להתעכב אם יש מחלוקת".

---

## 6. UI/UX requirements (the button)

The selector is the safety-critical surface. Non-negotiables:
- **Always visible** (header), not buried in a tab.
- Shows **venue** (Polymarket vs ₿ Binance) with distinct color + icon.
- Shows **money state** (demo/testnet = green "כסף מזויף" ; real = red "כסף אמיתי") — impossible to confuse.
- Shows **data source** so stats/demo numbers are never misattributed.
- Every order confirmation and position row is **tagged with the venue**.

Visual mockups to be produced during implementation (optionally via the brainstorming visual companion) before finalizing the header layout.

---

## 7. Testing plan
- **Phase 1:** unit tests for the source resolver (each price/PTB/settlement function returns the selected source); a test proving demo and live settle off the *same* selected source (closes the current inconsistency); UI test that the badge reflects `data_source`.
- **Phase 2:** unit tests for `PredictFunVenue` order build/sign (against `predict-sdk`); a **testnet end-to-end** integration test (discover BTC 5m window → place → fill → read position → redeem); fault-injection on the naked-position/dispute path; a test that real-money is refused unless all three locks are satisfied.

---

## 8. Milestones
1. **M1 (Phase 1):** `data_source` field + resolver + engine wiring + settlement-consistency fix + header toggle + stats badge. Ship. Owner sees Binance-based demo/stats immediately.
2. **M2 (Phase 2a):** `Venue` seam + `PredictFunVenue` **read-only** on testnet (market discovery + book). Confirms we're on the right CLOB book. No orders.
3. **M3 (Phase 2b):** Predict.fun **testnet trading** end-to-end (fake USDT). Unified button. Prove the flow + observe the 2% fee impact.
4. **M4 (Phase 3):** real-money capability behind the triple lock + loss caps. Off by default. Only after testnet proves the mechanics.

---

## 9. Open questions / verify-during-build
- Predict.fun **testnet collateral (test USDT) faucet** — not documented; derive from testnet contract constants.
- Whether the hosted **matching API geo-filters** Israel independently of the (Israel-allowed) ToS.
- **Event Rush vs CLOB**: confirm the API book == the Binance-Wallet book before scaling (M2 gate).
- Exact `feeRateBps` per BTC market (read live).
- Header layout / exact visual of the money-state indicator (mockup during M1).
