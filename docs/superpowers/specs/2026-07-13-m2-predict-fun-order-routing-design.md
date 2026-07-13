# Design: M2 — route real ORDERS to Binance up/down (Predict.fun), testnet-first

- **Date:** 2026-07-13
- **Status:** Draft — awaiting owner review
- **Branch context:** `main` (M1 merged); M2 to be built on a new feature branch off `main`
- **Depends on:** M1 (data-source toggle) — DONE & merged to local `main`
- **Parent spec:** [2026-07-13-binance-data-source-and-venue-toggle-design.md](2026-07-13-binance-data-source-and-venue-toggle-design.md) §5 (M2 outline)
- **Related memory:** [[binance-bot-and-learning-advisor]], [[martingale-incident-and-faults-tab]], [[polymarket-compliance]], [[chainlink-price-feed]]

---

## תקציר לבעלים (Hebrew executive summary)

היום המערכת יודעת **לקרוא** נתוני BTC גם מ-Polymarket וגם מ-Binance (זה כבר עובד — שלב M1). עכשיו M2 מוסיף את החצי השני: **לשלוח פקודות אמיתיות** ל-"Binance up/down", שבפועל הוא **Predict.fun** — בורסת CLOB בסגנון Polymarket על בלוקצ'יין BNB, שהיא זו שמפעילה את "Event Rush" בארנק של Binance.

איך זה בנוי בבטחה:

- **מוסיפים "מתאם בורסה" (Venue).** אותה אסטרטגיה בדיוק — אותו זיהוי חלון, אותו חישוב סיכון, אותו FLW/מרטינגייל — רק ש"לאן הפקודה נשלחת" הופך לניתן-להחלפה. Polymarket נשאר בדיוק כמו שהוא (רק עוטפים אותו); Predict.fun הוא היעד החדש.
- **מתחילים בטסטנט (כסף מזויף).** ה-API של Predict.fun בטסטנט פתוח לגמרי, בלי מפתח, ואימתנו חי (2026-07-13) שיש בו שוקי BTC 5 ו-15 דקות. קודם קוראים בלבד ומוודאים שאנחנו על הספר הנכון, אחר כך סוחרים בכסף מזויף מקצה לקצה.
- **כסף אמיתי כבוי כברירת מחדל, מאחורי שלושה מנעולים** (בדיוק כמו Polymarket ו-Binance): (1) מתג-הריגה בסביבה `PREDICT_LIVE=1`, (2) המתג הידני "כסף אמיתי" באפליקציה, (3) מפתח ארנק קיים. חסר ולו אחד — נשארים בטסטנט. בנוסף יש **שומר-אי-התאמה**: אסור להחליט על מקור-נתונים אחד ולסחור על בורסה אחרת.

**האמת שחייבים לומר (לא משתנה):** זו אותה אסטרטגיה בלי יתרון מוכח, ועמלת ~2% ב-Predict.fun אוכלת את הקצה. הכפתור *לא* יוצר רווחיות — הוא נותן בחירת בורסה ובהירות. לכן M2 מתחיל בטסטנט וכסף אמיתי כבוי.

**תיקון חשוב לספֶק ההורה:** שוקי ה-BTC האלה **לא** עוברים חלון-מחלוקת UMA של ימים. הם נסגרים אוטומטית תוך שניות דרך `ChainlinkUpDownAdapter`. עדיין ייתכן עיכוב-הכרעה קצר, ולכן שומר ה-SETTLE_UNKNOWN (מהתקרית של −85%) נשאר — אבל אין "מחלוקת של שבוע".

---

## 1. Goals & non-goals

### Goals
1. Introduce a **venue-adapter seam** so `order_venue = "polymarket" | "predict_fun"` routes real ORDER placement (and the market discovery + order book it depends on) to Predict.fun on BNB Chain, while **every strategy decision path stays byte-for-byte identical**.
2. **Testnet-first, in two sub-phases:** M2a = `PredictFunVenue` read-only on BNB testnet (discover + book + top-of-book), which also verifies the "same book as Binance Event Rush?" gate; M2b = full testnet order lifecycle end-to-end with fake USDT.
3. Real money stays **OFF by default** behind the same **triple lock** as Polymarket/Binance, plus a **data/execution mismatch guard** and **per-venue loss caps**.
4. Never weaken any existing safety, and **never claim profitability** — the ~2% taker fee (`feeRateBps:200`) and the standing "no proven edge" verdict hold.

### Non-goals
- Any new trading edge. M2 changes *where orders go*, not EV.
- Leverage / futures. Predict.fun up/down is fixed-payout ($1 winner / $0 loser), like Polymarket. The Binance Futures cockpit (`engine/binance_cockpit.py`) stays a separate manual tool.
- Mainnet real-money trading in M2. That is M3 (this spec designs the gate; it is off by default).
- Rewriting `live_clob.py` or `market_discovery.py`. `PolymarketVenue` wraps them **unchanged** (pure delegation).

---

## 2. Verified ground truth (live 2026-07-13, testnet + SDK source)

| Thing | Testnet (M2/M2b) | Mainnet (M3 only) |
|---|---|---|
| REST base | `https://api-testnet.predict.fun` | `https://api.predict.fun` |
| Chain | BNB **testnet, chainId 97** | BNB **mainnet, chainId 56** |
| RPC | `https://bsc-testnet-dataseed.bnbchain.org/` | `https://bsc-dataseed.bnbchain.org/` |
| API key | **None** (reads + auth verified HTTP 200 with no key) | `x-api-key` (Discord-issued), 240 req/min |
| Collateral | **USDT (18 decimals!)** `0xB32171ecD878607FFc4F8FC0bCcE6852BB3149E0` — reports `TrueUSD`/`EUSD`/18 | `0x55d398326f99059fF775485246999027B3197955` |
| `CTF_EXCHANGE` (order `verifyingContract` for BTC up/down) | `0x2A6413639BD3d73a20ed8C95F634Ce198ABbd2d7` | `0x8BC070BEdAB741406F4B1Eb65A72bee27894B689` |
| `CONDITIONAL_TOKENS` (ERC1155) | `0x2827AAef52D71910E8FBad2FfeBC1B6C2DA37743` | `0x22DA1810B194ca018378464a58f6Ac2B10C9d244` |

**Confirmed live:** `GET /v1/markets?marketVariant=CRYPTO_UP_DOWN&status=OPEN` → 20 open BTC markets. Real 5m sample: `id 778011`, `conditionId 0x40c806…`, `feeRateBps 200`, `isNegRisk false`, `isYieldBearing false`, `decimalPrecision 2`, outcomes `Up`(indexSet 1, `onChainId 45899948…`) / `Down`(indexSet 2, `onChainId 24946845…`), oracle CHAINLINK BTCUSDT, `variantData.startPrice` = price-to-beat. A 15m variant (Pyth BTC_USD) is also live.

**Three facts that must not be copied from the Polymarket path:**
1. **USDT is 18 decimals** on BSC (and the testnet mock), NOT 6. All `*_wei` amounts and `pricePerShare` are 1e18-scaled. The SDK default `precision=18` is correct. Do **not** reuse Polymarket's 6-decimal USDC math.
2. **Up/Down → tokenId must be matched by outcome NAME**, not array index. Polymarket relies on `clobTokenIds[0/1]`; Predict's `outcomes[]` order is not guaranteed. A naive index map could silently invert direction — a correctness bug for FLW/chop.
3. **Contract addresses, chainId, and `verifyingContract` differ per chain** and must be read from `ADDRESSES_BY_CHAIN_ID[chain]`, never hardcoded.

**Settlement correction to the parent spec §2.2:** these `CRYPTO_UP_DOWN` BTC markets **auto-resolve via a dedicated `ChainlinkUpDownAdapter` off Chainlink Data Streams v3 in seconds**, NOT the UMA dispute window. UMA's ~2h→1week flow is the backstop for *generic* (non-crypto) markets only. Status flow is `REGISTERED → PRICE_PROPOSED → resolved`. Resolution can still **lag briefly** and an admin/Binance fallback exists, so the `SETTLE_UNKNOWN` guard (the −85% martingale-incident class) stays — but the UI must not overstate a "week-long dispute."

---

## 3. Venue-adapter architecture

### 3.1 The load-bearing insight
An order is placed against a `token_id`, and those ids come from discovery (`discover_active_btc_window`) and are priced via the book (`fetch_best_bid_ask` / `get_clob_book`). Predict.fun has its **own** `conditionId` / `onChainId(tokenId)` namespace and its **own** book. So you cannot route only `place_*_order` — discovery + book + placement are **one coupled triple** that must move together. (This is *unlike* M1's `data_source`, which swaps only the independent BTC-spot feed.)

### 3.2 Module layout (new)
```
engine/venues/
  __init__.py     # get_venue(name)->Venue singleton registry; VALID_ORDER_VENUES
  base.py         # Venue ABC/Protocol + ActiveMarket re-export + result typedefs + normalize()
  polymarket.py   # PolymarketVenue — pure delegation (live_clob.py & market_discovery.py UNCHANGED)
  predict_fun.py  # PredictFunVenue — NEW; testnet-first
engine/predict_secrets.py   # NEW — thin clone of binance_secrets.py, dedicated keyring service
engine/predict_equity.py    # NEW — clone of binance_equity.py, separate state file (M2b/M3)
```

### 3.3 The `Venue` interface (enumerated from real call sites)
| Method | Wraps today (Polymarket) | Purpose |
|---|---|---|
| `discover(window) -> ActiveMarket \| None` | `market_discovery.discover_active_btc_window` | token ids / epoch / window_sec / min size |
| `best_bid_ask(token_id) -> (bid, ask)` | `strategy_runner.fetch_best_bid_ask` (WS+CLOB, moves into `PolymarketVenue`) | every entry/exit/TP/peak read |
| `get_book(client, token_id) -> dict` | `market_discovery.get_clob_book` | signals, contract prices, min size |
| `place_entry_order(token_id, contracts, price, side, *, order_mode, entry_slippage_pct) -> dict` | `live_clob.place_entry_order` | BUY (auto/semi/hedge/DCA) |
| `place_exit_order(token_id, contracts, bid, *, order_mode, exit_slippage_pct, retry_max_attempts) -> dict` | `live_clob.place_exit_order` | SELL (TP/active-close) |
| `fetch_portfolio(*, force=False) -> dict` | `live_clob.fetch_live_portfolio` | reconcile + `/api/live/portfolio` |
| `fetch_chain_shares_for_token(token_id) -> float \| None` | `live_clob.fetch_chain_shares_for_token` | phantom-share sync on `insufficient_onchain_balance` |
| `fetch_account() -> dict` | `live_clob.fetch_polymarket_clob_account` | `/api/live/clob-account` |
| `reset_caches()` | `live_clob.reset_portfolio_cache` + `reset_trading_client_cache` | after key change |
| `live_disabled_reason() -> str \| None` | `live_clob._live_disabled_reason` | per-venue kill-switch |
| props: `name`, `is_testnet`, `collateral` (`"USDC"`/`"USDT"`), `chain_id` | new | UI badge + confirmation tags |

**Return-shape contract (unchanged so callers never branch):**
- order result: `{ok, error?, error_code?, order_id?, fill_price?, price?, size?, matched?}`
- portfolio: `{ok, balance_usd, positions:[{token_id, side, size, avg_price, mark_price, value_usd}], equity_usd, address, funder_address, is_proxy, hint}`
- `ActiveMarket`: reuse the existing dataclass (`market_discovery.py:33-49`). Do **not** fork it.

### 3.4 Exact touch points

**`engine/strategy_runner.py`** — add `self._venue` + `select_venue(name)` on `StrategyRunner.__init__` (~523-532); the runner reads `self.venue` fresh each tick so a mid-session switch takes effect at the next window without restart. Reroute:
- order/portfolio (`live_clob.*` → `self.venue.*`): `place_exit_order` @651, @2006; `fetch_live_portfolio` @755; `place_entry_order` @810, @906, @2213, @2653; `fetch_chain_shares_for_token` @2040; drop the `import live_clob` @15.
- discovery/book (module funcs → `self.venue.*`): `discover_active_btc_window` @1350, @1424; `fetch_best_bid_ask` calls @646, 900, 1669, 1670, 1863, 2003, 2391, 2433, with its **definition** @505-520 moving into `PolymarketVenue`; import @17.
- `seconds_until_window_end(m.epoch, m.window_sec)` @1665 stays a venue-neutral free function.
- add `order_venue` to `StrategyConfig` beside `data_source` @109.

**`engine/main.py`** — order/account sites route through the active venue: `fetch_polymarket_clob_account` @2461 (`/api/live/clob-account`), `fetch_live_portfolio` @2471 (`/api/live/portfolio`), `live_place_entry_order` @2492 (`/api/live/order`), `reset_portfolio_cache`+`reset_trading_client_cache` @2030-2031 & @2066-2067 (after key set/clear), import @89-96. The 9 UI/data discovery calls (`discover_active_btc_window` @391,562,697,1153,1329,2510,2579,3160,3341; `get_clob_book` @2524-2525) are DATA/UI reads that may stay on Polymarket for M2a; add a staged `_active_venue()` accessor so UI windows/prices match the trading venue before M2b (else the badge could mislead).

### 3.5 `order_venue` config field — same 5 places as `data_source`
1. Dataclass — `strategy_runner.py:109` (`order_venue: Literal["polymarket","predict_fun"] = "polymarket"`).
2. `ConfigBody` — `main.py:1613`.
3. POST validation — `main.py:1661-1662` (reject if not in `VALID_ORDER_VENUES`) + explicit set @1698-1713 calling `runner.select_venue(...)`.
4. Persist dict — `_save_persisted_config` @437; load @382-383. **Decision:** like `mode`, do **not** auto-restore into a live state — restore the field for display but keep live OFF (triple lock). Recommend `order_venue` resets to `polymarket` on restart unless deliberately re-armed.
5. GET response — `get_strategy_config` @1771.

New `/api/order-venue` endpoint modeled on `/api/data-source` (main.py:1848-1865); `runner.select_venue(name)` sets `self._venue = get_venue(name)`.

### 3.6 Composition with `data_source` (the single header button)
`order_venue` and `data_source` are **independent server-side fields**, but the unified header button writes **both together** because Predict.fun settles off Binance-family data and Polymarket off Chainlink: `Polymarket → {order_venue:polymarket, data_source:polymarket}`, `Binance → {order_venue:predict_fun, data_source:binance}`. Keeping them independent server-side lets a power user read Binance spot while trading Polymarket for A/B research; the button's happy path keeps them aligned and the **mismatch guard** (§5.3) is the backstop.

### 3.7 Market-model mapping (`PredictFunVenue.discover`)
| `ActiveMarket` field | Predict.fun source |
|---|---|
| `slug` | `categorySlug` (`btc-updown-5m-{epoch}` — same convention as our engine) |
| `epoch` | `int(categorySlug.rsplit("-",1)[-1])` (== `boostStartsAt`) |
| `window_sec` | 300/900 from the `5m`/`15m` prefix (or `boostEndsAt − boostStartsAt`) |
| `condition_id` | `conditionId` |
| `token_up` | `onChainId` of outcome **named "Up"** (indexSet 1) — **match by name, not index** |
| `token_down` | `onChainId` of outcome **named "Down"** (indexSet 2) |
| `outcome_prices` | inlined `outcomes[].bestBid/bestAsk` or `variantData.startPrice` (display only) |
| `order_min_size` | flat 1 USDT min (docs); `shareThreshold`/`spreadThreshold` present per market |
| `end_date_iso` | `boostEndsAt` |
| `closed` | `status`/`tradingStatus` |

`discover`: `GET /v1/markets?marketVariant=CRYPTO_UP_DOWN&status=OPEN` → filter by `categorySlug`/epoch for the current window whose `[open,close)` contains `now`; gate entries on `tradingStatus == "OPEN"`; cache with the same TTL/stale-on-open pattern (respect 240 req/min). `best_bid_ask`/`get_book`: top-of-book is **inlined** in the market object (`outcomes[].bestBid/bestAsk`); full depth via `GET /v1/markets/{id}/orderbook` is **single, Yes(Up)-based** — derive the Down side as `(1 − price)`. Upcoming windows return `bestBid/bestAsk = null` until liquidity arrives — **never treat null as a tradable price**.

---

## 4. Predict.fun integration recipe (auth + EIP-712 + lifecycle + testnet)

> The `predict-sdk` (PyPI, `PredictDotFun/sdk-python`) does **only** (a) EIP-712 order build+sign and (b) on-chain approvals/redeem/merge. It does **NOT** do REST or auth. `PredictFunVenue` writes auth + order submission + reads over plain HTTPS (via `httpx`), then uses the SDK to build/sign the order.

### 4.1 Auth — JWT handshake (plain EIP-191 personal_sign, NOT EIP-712)
Use an **EOA (raw private key) bot**, not a Predict Account smart wallet (`SignatureType.EOA=0` is the only signature type defined).
1. `GET {BASE}/v1/auth/message` → `{data:{message:"Please sign this message to log in… Timestamp:<ms>"}}`. Treat `message` as **opaque** (format may change; it is time-bound).
2. Sign with the EOA: `acct.sign_message(encode_defunct(text=message)).signature.hex()`.
3. `POST {BASE}/v1/auth {signer, signature, message}` → `{data:{token: <JWT>}}`.
4. Send `Authorization: Bearer <JWT>` on order actions & account reads (both networks); `x-api-key` on mainnet only. On 401, re-run the handshake.

Public reads (markets/orderbook) need no JWT on testnet.

### 4.2 Order signing — EIP-712 via the SDK (do not hand-roll)
- **Domain:** `name="predict.fun CTF Exchange"`, `version="1"`, `chainId` 97/56, `verifyingContract = CTF_EXCHANGE` (BTC up/down is `isNegRisk:false, isYieldBearing:false` → plain exchange; passing the wrong exchange = invalid signature).
- **`Order` struct — 12 fields:** `salt, maker, signer, taker, tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps, side(0=BUY/1=SELL), signatureType(0=EOA)`. `taker` = zero-address for public orders (**do not drop it**). `tokenId` = the outcome's `onChainId`. `feeRateBps` = read **live per market** (200); a stale fee is rejected.
- **SDK sequence:** `OrderBuilder.make(ChainId.BNB_TESTNET, PK)` → `get_limit_order_amounts(...)` / `get_market_order_amounts(MarketHelperInput(...), book)` → `build_order("LIMIT"|"MARKET", ...)` → `build_typed_data(order, is_neg_risk=False, is_yield_bearing=False)` → `sign_typed_data_order(typed)` (+ `build_typed_data_hash(typed)`). All amounts 1e18-scaled.

### 4.3 Order lifecycle (REST; placement is gasless)
- **Place — `POST /v1/orders`** `{data:{order:{…signed…, signature, hash}, strategy:"LIMIT"|"MARKET", pricePerShare, slippageBps, isMinAmountOut, isFillOrKill}}` → `{data:{code:"OK", orderHash}}`. FOK = `isFillOrKill`; **postOnly / time-in-force are undocumented** → approximate post-only with a non-crossing LIMIT and confirm empirically on testnet.
- **Open/filled — `GET /v1/orders?status=OPEN|FILLED`**; **Fills — `GET /v1/orders/matches`**; **Positions — `GET /v1/positions`**; **Book — `GET /v1/markets/{id}/orderbook`**.
- **Cancel — `POST /v1/orders/remove {data:{ids:[…]}}`** (gasless off-book; `noop` = already filled). On-chain cancel via SDK `cancel_orders` bumps the nonce (costs tBNB) — only for guaranteed invalidation.
- **WebSocket:** confirmed topics `predictOrderbook/{id}` and `predictMarketStatus/{id}` (watch for settlement). Account-scoped order/fill/position topics + testnet WS base URL are **undocumented** → **poll REST** for account state in M2b.
- Replicate `live_clob`'s 10s `wait_for` + error-code mapping (`post_order_timeout`, `insufficient_onchain_balance`, `min_order_size`).

### 4.4 On-chain bits & gas
- **Gasless:** placing and off-book cancel (operator settles on-chain).
- **Costs tBNB:** one-time approvals (ERC20 USDT allowance + ERC1155 operator approval on `CONDITIONAL_TOKENS`, spender = `CTF_EXCHANGE`) via `run_approvals(get_approval_steps(ApprovalScope("TRADE", False, False)))` (idempotent); `redeem_positions(conditionId, index_set=winning, is_neg_risk=False, is_yield_bearing=False)`; `merge_positions`; optional on-chain cancel.

### 4.5 Testnet setup & the test-USDT problem
Base `https://api-testnet.predict.fun`, chain 97, **no API key**. tBNB for gas from the BNB testnet faucet.
- **Test USDT (EUSD, `0xB32171…49E0`, 18-dec) is the M2b blocker:** the token is a proxy with **no public mint/faucet selector** (6 common selectors checked, none present). Acquisition path is **NOT documented**; resolve during M2b in priority order: (1) Predict.fun testnet dApp "get test funds"/deposit faucet after connecting the EOA; (2) Predict.fun Discord ticket with the EOA address; (3) inspect the EIP-1967 impl for a role-gated `mint`. **M2a read-only needs neither collateral nor gas** and is fully unblocked today.

---

## 5. Safety model

### 5.1 The triple lock (`engine/predict_secrets.py`, a thin clone of `binance_secrets.py`)
Dedicated keyring service `predict-fun-wallet` so the wallet key can never collide with `POLYMARKET_PRIVATE_KEY` or the Binance blob. `predict_secrets.is_live_enabled()` is True **only if ALL THREE hold** (adopts the stricter Binance form):
1. **Env kill-switch `PREDICT_LIVE == '1'`** — absent/any other value/`=0` ⇒ testnet only. Plus a **default-safe testnet flag** `PREDICT_TESTNET`: only `PREDICT_TESTNET ∈ {0,false,no,off}` points at BNB mainnet (chain 56); anything else ⇒ testnet (chain 97). So `is_live_enabled = (PREDICT_LIVE=='1') AND has_wallet_key() AND not is_testnet()`.
2. **The single in-app "real money" toggle** (`rt.live_trading`, `POST /api/live/mode` — the only in-app arming path). The unified venue button decides which venue the one switch arms. Not auto-restored into a live state on restart.
3. **Wallet key present** (env `PREDICT_WALLET_KEY` OR the dedicated secret_store). Only presence booleans are ever exposed — never the key.

**Make `strategy_runner._live_trading_ok()` venue-aware:** when `order_venue == "predict_fun"` it delegates to `predict_secrets.is_live_enabled()`. This single choke point already gates all ~7 live call sites, so one change protects every one without touching the Polymarket path.

**Fail-closed refusal:** `PredictFunVenue.place_entry/exit` calls `is_live_enabled()` first; False ⇒ route to the **BNB testnet client** (fake money) or refuse + record a fault — a real order is **never** placed silently (mirrors `_get_binance_client(force_testnet=…)`).

### 5.2 Per-venue loss caps & reconcile-on-restart
- **`engine/predict_equity.py`** (clone of `binance_equity.py`) persisted to `DATA_ROOT/predict_equity_state.json` — **separate state** so Polymarket and Predict.fun drawdowns are tracked independently. Feed `risk_engine.gate_order` before every real Predict.fun entry; add an **equity floor** that refuses entries below a configured collateral level (off-by-default until the owner sets one).
- **Reconcile:** clone `_live_reconcile_if_enabled` (boot + ~120s) via `PredictFunVenue.fetch_portfolio` (real USDT + on-chain ERC1155 positions), with the F8 repeated-failure fault backstop. A resolved-but-not-yet-redeemed winner must show **"open, awaiting resolution"**, never a realized loss (`SETTLE_UNKNOWN` class — the −85% martingale-incident guard stays).

### 5.3 The data/execution MISMATCH GUARD (oracle-divergence rule)
Before ANY real-money order, fail-closed:
```
if predict_secrets.is_live_enabled() and data_source.get_active() != order_venue_data_domain(order_venue):
    REFUSE (fall back to testnet/blocked) + high-severity fault + red UI banner
```
where `order_venue_data_domain("predict_fun") == "binance"` and `("polymarket") == "polymarket"`. In plain terms: **real Predict.fun orders only while `data_source=="binance"`; real Polymarket only while `data_source=="polymarket"`.** Any mismatch blocks the real order. This sits inside (or immediately after) the venue-aware `_live_trading_ok()` so no call site can bypass it. The unified button keeps both aligned; the guard backstops a manual/API desync or stale restart.

### 5.4 UI (safety-critical surface)
Extend `_live_mode_state()` into a venue-aware state the header polls:
- **Venue badge:** "Polymarket" vs "₿ Binance (Predict.fun)", distinct color + icon; every order confirmation + position row venue-tagged.
- **Money state:** RED "כסף אמיתי" only when `is_live_enabled()` is truly effective (all three locks); otherwise GREEN "כסף מזויף / טסטנט". Data source shown alongside so numbers are never misattributed.
- **Mismatch banner (red):** "מקור הנתונים והבורסה לא תואמים — מסחר אמיתי חסום".
- **Settlement-lag notice (Predict.fun only, corrected wording):** these BTC markets resolve fast via Chainlink; show a mild "ההכרעה מתבצעת אוטומטית תוך שניות; ייתכן עיכוב קצר" and, for any unsettled row, "ממתין להכרעה" rather than a final P&L. Do **not** claim a week-long UMA dispute for BTC markets.
- **`reason_blocked`** names exactly which lock is open (e.g. `PREDICT_LIVE ≠ '1'`, "אין מפתח ארנק", "מצב טסטנט").

### 5.5 Never claim profitability (restated)
The ~2% taker fee + no proven edge stand; advisor/UI must not claim profitability while net EV ≤ 0. Chop-armed FLW (martingale) on real money keeps the `fix/martingale-blowup-guard` guard and is surfaced, never silently combined with the new venue. Real money OFF by default.

---

## 6. Phased milestones

**M2a — `PredictFunVenue` read-only on testnet (no orders, no risk).**
1. `engine/venues/base.py` (`Venue` ABC, `ActiveMarket` re-export, `VALID_ORDER_VENUES`, `normalize`).
2. `PolymarketVenue` (pure delegation) + `get_venue` registry; wire `runner.select_venue`; reroute the runner's `live_clob.*` + `fetch_best_bid_ask` + `discover` calls through `self.venue`; move `fetch_best_bid_ask` body into `PolymarketVenue`. **Verify Polymarket behavior is byte-identical** (pure refactor).
3. `order_venue` field in the 5 places + `/api/order-venue` + unified-button plumbing (still defaults `polymarket`).
4. `PredictFunVenue.discover` + `get_book` + `best_bid_ask` on BNB testnet (chain 97, no key). Assert Up/Down **label→tokenId** mapping. **Verify the "same book as Binance Event Rush?" gate** (evidence currently leans DIFFERENT/bonding-curve — cross-check `conditionId`/`onChainId` on BscScan + CLOB spread vs single curve price). Order methods raise `NotImplementedError` guarded by `live_disabled_reason`.

**M2b — testnet order lifecycle end-to-end (fake USDT, behind the triple lock).**
5. Obtain test EUSD (§4.5). Implement `place_entry_order`/`place_exit_order` (SDK build+sign → REST POST), `fetch_portfolio`, `fetch_chain_shares_for_token`, `redeem_positions`, `reset_caches`, `live_disabled_reason`; `engine/predict_secrets.py` + `engine/predict_equity.py`; venue-aware `_live_trading_ok()`; mismatch guard; reconcile clone. Match the result-shape contract so the runner needs **zero** branching.
6. Testnet dry-run: run several 5m/15m windows — FOK entry + limit/FAK exit, reconcile, settlement/redeem. Confirm phantom-share + `insufficient_onchain_balance` handling, and observe the 2% fee impact.

**M3 — real-money gate (mainnet, chain 56).** OFF by default. Gate behind `PREDICT_MAINNET`/`PREDICT_TESTNET=0` + `PREDICT_LIVE=1` + in-app toggle + funded mainnet wallet key + Discord-issued `x-api-key`. Only after M2b proves the mechanics, per-venue loss cap/equity floor set, mismatch guard clear. Legal/tax sign-off (project memory) is a human prerequisite the code cannot enforce.

---

## 7. Testing plan

**Unit (no network):**
- `Venue` seam: `PolymarketVenue` returns byte-identical results to the pre-refactor call sites (golden-output comparison on discover/book/order-result shapes).
- `order_venue` config round-trip through the 5 places; `/api/order-venue` validation rejects unknown venues.
- `PredictFunVenue.discover` field mapping incl. the **Up/Down-by-name** assertion (feed outcomes in reversed array order → direction must not invert).
- 18-decimal amount math (assert no 6-decimal contamination).
- Triple-lock truth table: `is_live_enabled()` False unless all three of `PREDICT_LIVE=='1'` + wallet key + `not is_testnet()`; `reason_blocked` names the open lock.
- Mismatch guard: real order refused when `data_source != order_venue_data_domain`.

**Testnet e2e (chain 97, fake USDT):**
- M2a: discover the live BTC 5m/15m window, read book, confirm `conditionId`/`onChainId` + same-book gate.
- M2b: one full window — approvals → auth → FOK entry → fill read → exit/redeem → reconcile. Confirm the 10s `wait_for` + `post_order_timeout`/`insufficient_onchain_balance`/`min_order_size` mapping matches `live_clob`.

**Fault-injection (highest-risk new paths):**
- Naked position / `insufficient_onchain_balance` → phantom-share chain sync recovers, no double-buy.
- Resolved-but-not-redeemed winner → reconcile shows "awaiting resolution", **never** scored as a loss, and does **not** feed the martingale (regression test for the −85% incident class).
- Settlement lag: `PRICE_PROPOSED` held open across a reconcile tick → position stays open, no premature settle.
- Kill-switch: `PREDICT_LIVE=0` mid-session → next real order refused immediately.

---

## 8. Owner setup checklist (non-technical)

You do **nothing** here until testnet proves the full flow. Real money stays off by default regardless.

### Now — TESTNET (free, safe, no real money, no API key)
1. Create/import a **BNB-chain wallet (EOA)** used **only** for this bot — copy its private key, treat it like a password. Do **not** reuse a wallet holding real funds.
2. Switch the wallet to **"BNB Smart Chain Testnet" (chainId 97)**.
3. Get free **test BNB (tBNB)** for gas from the official BNB testnet faucet.
4. Get **test USDT collateral** — via the Predict.fun testnet dApp faucet or a Discord ticket (exact source confirmed during M2b; no purchase, no real money).
5. Paste the testnet wallet key into the app's Predict.fun field (stored encrypted) **or** set env `PREDICT_WALLET_KEY`. Leave `PREDICT_LIVE` unset and `PREDICT_TESTNET` at its testnet default.
6. Select the **"₿ Binance (Predict.fun)"** venue; confirm the header shows GREEN "כסף מזויף / טסטנט"; let it run a few 5m/15m windows and confirm fills + settlement look right.

### Later — MAINNET / REAL MONEY (optional, off by default, only after testnet proves out)
7. Accept the reality first: same no-proven-edge strategy; ~2% taker fee eats the edge; the button does not create profit — only proceed with money you can afford to lose.
8. Fund a **fresh, dedicated** BNB-chain wallet with real USDT + a little real BNB for gas (not your main holdings).
9. Obtain the **Predict.fun mainnet API key** via their official Discord; store it in the app's encrypted key field.
10. Set the funded mainnet wallet key; set `PREDICT_LIVE=1` and `PREDICT_TESTNET=0` (chain 56) — absent either keeps the bot on testnet.
11. Set a **per-venue loss cap / equity floor** in the app **before** arming, so a bad run auto-halts.
12. Flip the in-app "real money" toggle ON, select the Predict.fun venue, confirm the header turns **RED "כסף אמיתי"**; do **not** trade if the red mismatch banner appears.
13. Know the settlement caveat: BTC markets resolve automatically in seconds; a brief lag may show "ממתין להכרעה" before final P&L.
14. Get a **lawyer's opinion** on Israeli legality/tax before real money. Never share or commit the wallet key or the API key. Set `PREDICT_LIVE=0` to hard-stop real trading instantly.

---

## 9. Open questions / verify-during-build
- **Test-USDT (EUSD) faucet** mechanism is undocumented — the M2b blocker (§4.5).
- **postOnly / time-in-force** on `POST /v1/orders` — only `isFillOrKill` documented; approximate with a non-crossing LIMIT, confirm on testnet.
- **Account-scoped WS topics** + testnet WS base URL — undocumented; poll REST for account state.
- **Same-book gate:** confirm the Predict.fun CLOB book == the Binance-Wallet "Event Rush" book (evidence leans DIFFERENT/bonding-curve) before scaling — M2a read-only.
- **Geo-filtering** of the hosted matching (write) API for Israel — reads worked from this environment; the write path is unconfirmed.
- **Exact `feeRateBps` per market** — read live (assumed 200/2%); a higher fee worsens the already-negative EV.
- **Data-domain mapping** (`predict_fun → "binance"`) assumes Predict.fun up/down resolves off Binance-family BTC data — confirm before real money or the mismatch guard could allow/deny incorrectly.
