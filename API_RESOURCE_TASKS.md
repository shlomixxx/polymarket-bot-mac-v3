# 🚀 חיסכון במשאבי Railway — אופטימיזציית קריאות API

> **נכתב עבורך בזמן שישנת (3-4 ביוני 2026).**
> ביצעתי ביקורת מקיפה של כל קריאות ה-API במערכת באמצעות 7 סוכני ניתוח במקביל,
> וכל ממצא עבר **אימות יריב (adversarial verification)** נפרד שבדק שתי שאלות:
> 1. האם זה באמת מאיט את המשתמש? (latency risk)
> 2. האם זה עלול להזין מידע ישן/שגוי להחלטת מסחר? (correctness risk)
>
> **44 ממצאים גולמיים → 37 אומתו כאמיתיים → 35 בטוחים לביצוע מיידי, 2 דורשים זהירות.**
>
> כל מה שמופיע כאן **מאומת מול הקוד** (file:line) ולא ספקולציה. כל משימה כוללת Guardrails מפורשים
> כדי לא לחזור על תקרית ה-martingale (ה-SETTLE_UNKNOWN שהזין את מנגנון ההכפלה).

---

## ✅ סטטוס ביצוע (עודכן — branch: `api-resource-optimization`)

בוצע ב-TDD על branch נפרד (production נשאר נקי עד אישור). **238 טסטים python + 8 frontend + tsc + vite build — הכל ירוק.** נוספו 38 טסטים חדשים (7 קבצים).

**בוצע ונבדק (✅):**
- **תשתית:** 0.1 `engine/_cache.py` (TTLCache + SingleFlight), 0.2 לקוח Binance keep-alive, 0.3 `engine/_http_cache.py` (ETag/304).
- **קבוצה A (כל ה-7):** A-1/A-2 snapshot 250→500ms + in-flight guard, A-3 snapshot win-rate פעם אחת + ETag/304, A-5 discovery מוחזק לאורך החלון, A-6 memoize ל-CLOB client (+reset על שינוי מפתח), A-7 cache מחירי settlement.
- **קבוצה B:** B-1 cache יתרת account, B-2 signals gather+cache, B-3 single-flight ל-get_clob_book, B-4 keep-alive ב-fetch_best_bid_ask, B-5 keep-alive בלולאות DCA/TP, B-6 לקוח Binance + gather, B-9 Page Visibility, B-13 last-window cache+ETag (+ביטול ב-POST config).
- **קבוצה C:** C-3 live/mode ETag, C-4 logs+log-entries ETag, C-6 Chainlink דרך לקוח Polygon מאוגד, C-10 FaultsTab isPageHidden, C-11 Fear&Greed 6h, C-12 funding 30min.
- **B-8:** הושג דרך memoization של win-rate (ETag לא ישים כי ב-config יש `last_tick_ts`/uptime שמשתנים בכל בקשה).

**נדחה במכוון (עם נימוק):**
- **D-1** (`/api/demo/state` — הסרת mark_to_market מהבקשה): 🔴 הפריט היחיד שסומן "לא בטוח כפי שנוסח". דורש לבנות לולאת mark ברקע **ולאמת בריצה חיה** שהיא מקדמת `last_mark` כשהמנוע OFF, לפני הסרת ה-mark מה-handler. נוגע ב-plumbing של settlement (מחלקת הבאג של ה-martingale) — מומלץ כשינוי נפרד עם בדיקה חיה.
- **D-2** (klines): אין גרסה שגם בטוחה וגם חוסכת — הגרסה החוסכת (יישור לגבול נר) מקפיאה את ה-momentum עד דקה. ה-TTL הנוכחי (15s) כבר בטוח. הושאר כמות-שהוא.
- **C-1/C-2** (orderbook-summary, contract-prices ETag): לא ישים — שניהם מחזירים שדה `ts` שמשתנה בכל בקשה (ETag לעולם לא יתאים). ה-WS cache בצד השרת כבר חוסך את קריאות ה-CLOB.
- **B-7, B-12, B-14, C-5, C-7, C-8, C-9, disc-3:** ערך נמוך יותר או refactor פרונטאנד/endpoint מעורב; נדחה לסבב הבא (פירוט בגוף הקובץ).

**הערה:** השינויים על branch — לא מוזגו ל-main ולא נפרסו ל-Railway. ראה סיכום בצ׳אט להוראות מיזוג/פריסה.

---

## 📊 תקציר מנהלים — מה מצאנו

המערכת היום **לא מבזבזת על שכפול מידע שכבר נשמר** ברוב המקרים, אבל יש 3 דליפות משאבים מרכזיות:

1. **ה-Frontend מציף את השרת.** טאב פתוח אחד שולח **~18 בקשות בשנייה** לשרת:
   - `snapshot` כל **250ms** = 4 בקשות/שנייה (וזה רץ פעמיים — גם ב-App וגם ב-LiveStream/OBS).
   - חבילת `refresh()` של ~11 endpoints כל **800ms** = עוד ~13.75 בקשות/שנייה.
   - העומס הזה רץ 24/7 על מסך ה-OBS גם כשאף אחד לא מסתכל.

2. **השרת מחשב מחדש כל פעם מאפס.** ה-`snapshot` מסדר מחדש 400 עסקאות + 2000 נקודות equity
   וסורק 50,000 עסקאות לחישוב win-rate **3 פעמים** בכל קריאה — 4 פעמים בשנייה. שום דבר מזה כמעט
   לא משתנה בין שתי קריאות סמוכות.

3. **קריאות חוץ חוזרות על מידע קבוע.** אותו slug של Polymarket נמשך מ-Gamma ~10 פעמים בכל חלון
   (למרות שהוא לא משתנה), מחירי ה-settlement של חלון נמשכים 3-4 פעמים (למרות שהם קבועים אחרי
   סגירת הנר), ו-Fear&Greed (שמתעדכן פעם ביום) נמשך 24 פעמים ביום. כל קריאת order חדשה גם בונה
   מחדש את ה-CLOB client ומבצעת auth handshake רשתי מלא.

### לוח לפני / אחרי (לכל טאב פתוח)

| מדד | היום | אחרי | חיסכון |
|---|---|---|---|
| בקשות לשרת / שנייה / טאב | ~18 | ~3-4 | **~80%** |
| `snapshot` poll | 4/שנייה | 1/שנייה | -75% |
| סריקות 50k עסקאות / שנייה | עד 24 | ~1-2 | **~90%** |
| קריאות Gamma discovery / חלון 5ד׳ | ~10 | ~1 | **~90%** |
| קריאות klines settlement / חלון | 3-4 | 1 | -70% |
| CLOB auth handshakes | לכל בקשה | ~0 (memoized) | **~95%** |
| Fear&Greed / יום | ~24 | ~4 | -83% |
| Funding rate / יום | ~288 | ~24-48 | -85% |

**התחושה למשתמש לא תיפגע:** מחיר ה-BTC החי וה-Up/Down כבר מגיעים מ-WebSocket / endpoint נפרד
שנשאר ב-≤750ms. כל ה-caching מוצע אך ורק על מידע תצוגתי שבו 250ms→1s **בלתי מורגש** (PnL, יתרה,
פוזיציות, equity). ברוב המקרים השינוי אפילו **מאיץ** את המערכת (פחות handshakes, פחות חישוב).

---

## 🛡️ עקרון הזהב + רשימת ה-Guardrails הקדושים

**העיקרון:** כל cache הוא ב**שכבת התצוגה (HTTP handler / frontend)** בלבד. אסור בתכלית האיסור
להחדיר cache לפונקציות הרמה-הנמוכה שההגיון המסחרי קורא להן ברגע הביצוע.

האימות-היריב סימן את הנתיבים הבאים כ**"לעולם לא ל-cache / חייב לעקוף"** — הם הקו האדום:

| נתיב | למה אסור ל-cache | הקובץ |
|---|---|---|
| `check_balance_before_order` → יתרת CLOB ברגע ההזמנה | יתרה ישנה תאשר order ללא כיסוי | `engine/live_clob.py:91-128` |
| `fetch_best_bid_ask` / `get_clob_book` ברגע ה-order | מחיר entry/exit ישן | `engine/strategy_runner.py:374`, `engine/market_discovery.py:351` |
| `demo_engine.best_ask` / `simulate_market_buy` / `simulate_sell_all` | מחיר ה-fill בפועל | `engine/demo_engine.py:787,822,1172` |
| מחירי open/close ל-settlement | אסור לאחסן `None`; רק ערך סופי אחרי סגירת נר | `engine/btc_price.py:260-291` |
| `fetch_btc_spot_usdt` TTL | momentum entry משתמש בו → חייב להישאר ≤1 שנייה | `engine/btc_price.py:199` |
| ה-open bar של klines | קופא את momentum_3m/5m עד 60 שניות → entry בכיוון שגוי | `engine/ta_signals.py` |
| `loss_recovery_streak` / `loss_recovery_multiplier` ב-config endpoint | חייב להישאר דינמי כדי לראות martingale בורח | `engine/main.py:1364-1365` |
| תפוגת גבול החלון ב-discovery (`seconds_until_window_end<=0 → None`) | קריטי ל-rollover תקין | `engine/market_discovery.py:165` |

**כלל אצבע:** אם פונקציה מוחזרת ב-`true` בשדה `feeds_trade_decision` — ה-cache עליה (אם בכלל)
חייב להיות sub-second או עם `force=True` bypass בנקודת הביצוע.

---

## 🧱 שלב 0 — תשתית משותפת (לבנות קודם, מאפשר את כל השאר)

ארבעת אלה הם הבסיס. לבנות אותם פעם אחת → כל המשימות שאחריהם נהיות קצרות ועקביות.

- [ ] **0.1 — מודול cache קטן משותף** `engine/_cache.py`
  - `TTLCache` (dict עם ts + TTL) + `single_flight` (מפת `dict[key -> asyncio.Future]` כך ששתי
    קריאות מקבילות לאותו מפתח מחכות לבקשה אחת).
  - מחליף את ה-caches האד-הוק הפזורים (`_PORTFOLIO_CACHE`, `_contract_price_cache`,
    `_CLOB_MIN_SIZE_CACHE`, `_CHAINLINK_AT_WINDOW_CACHE`) בכלי אחיד אחד.
  - תומך ב-`force=True` bypass — חובה לנתיבי ה-order.

- [ ] **0.2 — לקוחות HTTP משותפים עם keep-alive** (singleton per host group)
  - היום כל קריאת BTC/CLOB פותחת `httpx.AsyncClient()` חדש = TLS handshake מלא בכל פעם.
  - ליצור `_get_binance_client()`, `_get_clob_book_client()` בסגנון `_get_polygon_client`
    הקיים (`engine/btc_price.py:71-78`) עם `httpx.Limits(max_keepalive_connections>=4)`.
  - **בלי result cache** — רק שימוש חוזר בחיבור. מאיץ את כל הקריאות כולל ה-order path.

- [ ] **0.3 — עוזר ETag/304 ל-FastAPI** (אין כזה היום ב-`main.py`)
  - helper שמקבל payload, מחשב `hash`, מציב `ETag`, ומחזיר `304 Not Modified` כש-`If-None-Match`
    תואם → גוף ריק נשלח.
  - ETag תמיד מחושב על ה-payload **הנוכחי** (לא מוקפא בטיימר) כדי שדגלי kill-switch/שינויי מצב
    ישתקפו מיד.
  - מותקן על כל ה-GET התצוגתיים: snapshot, state, market/current, orderbook-summary,
    contract-prices, signals, config, live/mode, logs, last-window-outcome.

- [ ] **0.4 — לולאת רקע "חשב snapshot + mark פעם בטיק"** (מאפשר את snap-1 + state-1)
  - task ב-`main.py` lifespan שמריץ `mark_to_market()` כל ~250-500ms **ללא תלות במצב המנוע**
    (כי המצב מתאפס ל-OFF בכל restart, ואז שום טיק לא מריץ mark).
  - מעביר את הבעלות על `_backfill_missing_tp_settlement_btc()` + `save()` ללולאה הזו.
  - ⚠️ **חובה לאמת שהלולאה רצה ומקדמת `last_mark.ts` כשהמנוע OFF — לפני** שמוציאים את
    ה-`mark_to_market` מתוך ה-handlers (ראה משימה D-1).

---

## 🅰️ קבוצה A — רווח ענק, עדיפות עליונה

> אלו 7 השינויים עם יחס הרווח/סיכון הטוב ביותר. אם תעשה רק את הקבוצה הזו — תוריד ~70% מהעומס.

> ### 🔴 שים לב — האחוז החי של העסקה (חלון מקופל בסטטיסטיקה דמו + לייב)
> האחוז שזז בלייב מגיע מ-`last_mark.legs.unrealized_pct`, שמחושב **בצד השרת** ב-`mark_to_market`
> ונקרא דרך ה-`snapshot` poll. **A-1/A-2 הם הכפתור היחיד שמשפיע עליו.**
> - **תקינות:** 100% בטוח — החישוב לא משתנה, לעולם לא תראה מספר שגוי.
> - **חלקות:** כשהבוט **פועל**, האחוז זז היום 4×/שנייה; ב-1000ms יזוז 1×/שנייה (עדיין חי, קפיצות
>   מעט גדולות יותר). כשהבוט **כבוי** — כבר זז ~1×/שנייה היום, אז אין שינוי.
> - **המלצה כדי לא לאבד את התחושה:** עשה את **A-3 במלואו** (שם הרווח האמיתי ב-CPU, בלתי נראה לך),
>   ולגבי ה-interval בחר **500ms** במקום 1000ms (חלק, וחוצה את התעבורה) — או השאר 250ms, כי עם
>   ה-ETag/304 של A-3 כל poll שבו כלום לא השתנה עולה כמעט אפס. סריקת ה-50k הכבדה נעלמת בכל קצב.
> - **בונוס:** לולאת ה-mark ברקע (תשתית 0.4 / D-1) תרענן את `last_mark` כל 250-500ms **גם כשהבוט
>   כבוי** — כלומר האחוז יהיה אפילו **חלק יותר מהיום** במצב כבוי.

- [ ] **A-1 · `poll-1` — להאט את ה-snapshot poll מ-250ms ל-500ms (App)** `src/App.tsx:2044-2068`
  - **היום:** `setInterval(pollSnapshot, 250)` = 4 בקשות/שנייה רציף. ה-payload כבד (400 עסקאות +
    2000 נק׳ equity) וכמעט לא משתנה בין טיקים.
  - **תיקון:** שנה את ה-interval ב-`src/App.tsx:2066` ל-`500` (פשרה שמשמרת את חלקות האחוז החי —
    ראה ההערה האדומה למעלה; 1000ms רק אם החלטת שלא אכפת לך מהקפיצות), תקן את ההערה "Fast 500ms"
    בשורה 2044, והוסף `in-flight guard` (ref בוליאני עם `setTimeout` משורשר כמו ב-`refresh()` loop).
  - **Latency:** אין השפעה — מחיר ה-BTC מגיע מ-WebSocket נפרד; 250ms→500ms על PnL בלתי מורגש.
  - **Correctness:** קריאה תצוגתית בלבד; ה-engine לא קורא אותה.
  - **⚠️ תלוי ב-A-3:** עשה את A-3 (cache+ETag) **לפני/יחד** עם זה — A-3 הוא שנותן את רוב החיסכון
    ומאפשר להשאיר interval קצר בלי עלות. אל תאט בלי A-3.
  - **חיסכון:** 250→500ms = -2 בקשות/שנייה/טאב (250→1000ms = -3). **הזוכה הגדול ביותר בודד.**

- [ ] **A-2 · `poll-2` — אותו תיקון ל-LiveStream/OBS** `src/LiveStreamTrade.tsx:889-939`
  - **היום:** עוד `setInterval(pollSnapshot, 250)` על מסך ה-broadcast — שרץ 24/7 על מכונת OBS
    בלי שאף אחד צופה.
  - **תיקון:** interval 250→500ms (או 1000ms ל-OBS, שם בד"כ אין מי שנועץ עיניים באחוז) ב-
    `src/LiveStreamTrade.tsx:937`. ה-countdown הנראה לעין כבר מונפש מ-clock מקומי של 1 שנייה
    (שורה 942) + אינטרפולציה (שורות 1251-1258) — נשאר חלק בכל קצב.
  - **חיסכון:** -2 עד -3 בקשות/שנייה/טאב, **החיסכון הכי גדול ל-24/7** כי OBS רץ ללא השגחה.

- [ ] **A-3 · `snap-1` — cache + ETag ל-snapshot, וחישוב win-rate פעם אחת** `engine/main.py:1103-1134`
  - **היום:** כל קריאה (4-8/שנייה) בונה מחדש את כל ה-payload + קוראת `_bot_run_win_rate_stats()`
    **3 פעמים**, וכל קריאה סורקת עד 50,000 עסקאות באופן ליניארי.
  - **תיקון (לפי סדר רווח):**
    1. קרא ל-`_bot_run_win_rate_stats()` **פעם אחת** ושתף את התוצאה לשלושת המפתחות → מבטל מיד
       2 מתוך 3 סריקות ה-O(50k). זה הרווח הבודד הגדול.
    2. memoize את ה-payload המלא לפי token זול: `(rt.last_tick_ts, state.trade_seq,
       len(trades), len(equity_history))`.
    3. הוסף ETag/304 (תשתית 0.3) → polls זהים מחזירים גוף ריק.
  - **Guardrail:** ה-cache קריאה-בלבד; ה-runner ממשיך לקרוא `self.demo.state` ישירות בביצוע order.
  - **חיסכון:** סריקות win-rate יורדות מ-24/שנייה ל-~2; CPU של snapshot -80-90%.

- [ ] **A-4 · `poll-3` — חבילת refresh() מדורגת לפי תנודתיות + ביטול כפילות** `src/App.tsx:2028-2042, 1868-1890`
  - **היום:** ~11 GETs כל 800ms (≈13.75 בקשות/שנייה). `/api/demo/state` ו-`/api/demo/snapshot`
    מחזירים את אותם balance/positions/last_mark — כפילות. `/api/strategy/config` נמשך כל מחזור
    למרות שמשתנה רק בשמירה.
  - **תיקון:**
    1. **Tier מהיר** (≤1s): `/api/btc/live` + snapshot.
    2. **Tier בינוני** (≥1s): `/api/market/orderbook-summary` (כבר עושה self-cache 0.5s).
    3. **Tier איטי** (5-10s): `config`, `live/mode`, `polymarket-clob-account`,
       `last-window-outcome`, `logs`, `log-entries`, `pending`, `market/current`.
    4. **בטל את `/api/demo/state` מהלולאה החמה** — הרחב את snapshot שיכלול את השדות החסרים
       (`trade_seq`, `loss_recovery_streak/multiplier`, `stats_epoch_ts`, מוני DCA) או השאר את
       state ב-tier איטי. snapshot הוא היחיד שלא מריץ `mark_to_market`.
    5. הוסף `visibilitychange` listener שמרענן מיד בחזרה לטאב (ראה B-9).
  - **חיסכון:** -4 עד -6 בקשות/שנייה/טאב **+ מסיר `mark_to_market()` אחד לכל מחזור** (החיסכון
    הכי גדול ב-CPU של המנוע).

- [ ] **A-5 · `disc-1` — cache discovery מיושר לאורך החלון (לא 30s שטוח)** `engine/market_discovery.py:153-167, 256-277`
  - **היום:** `_DISCOVERY_TTL_SEC=30` → אותו slug אימ-יוטבילי נמשך מ-Gamma ~10 פעמים בחלון 5ד׳,
    ~30 פעמים בחלון 15ד׳. ה-slug/conditionId/tokenIds הם פונקציה דטרמיניסטית של ה-epoch ולא
    משתנים לכל אורך החיים של החלון.
  - **תיקון:** החזק את ה-`ActiveMarket` לשדות האימ-יוטביליים לאורך **גוף החלון** (TTL =
    `seconds_until_window_end`). **שמור את תפוגת הגבול כפי שהיא** — `_cached_market` חייב
    להמשיך להחזיר `None` ברגע ש-`seconds_until_window_end<=0` (שורה 165), בלי grace חיובי, אחרת
    rollover/settlement יתעכב. שמור sub-TTL של 30-60s רק לרענון `outcome_prices` (לתצוגה ב-
    `/api/market/current`).
  - **Guardrail:** אל תיגע ב-`fetch_best_bid_ask`/`get_clob_book` (entry קורא ask חי בשורה 1702);
    אל תיגע בנתיב ה-Binance של settlement; הוסף test שמוודא קריאת Gamma אחת בלבד לכל חלון פתוח.
  - **חיסכון:** -80-90% קריאות Gamma. הזוכה הגדול ביותר על תעבורת חוץ.

- [ ] **A-6 · `clob-1` — memoize את ה-CLOB trading client + creds** `engine/live_clob.py:33-88`
  - **היום:** כל `place_*_order` / `fetch_polymarket_clob_account` בונה `ClobClient` חדש ומריץ
    `create_or_derive_api_creds()` = **POST רשתי מלא** ל-CLOB בכל בדיקת יתרה, כל order, וכל poll
    של ה-endpoint. הדשבורד מפעיל את זה ~40-75 פעמים בדקה.
  - **תיקון:** memoize את `(ClobClient, creds)` ברמת המודול, מפתח = `(pk.strip(), signature_type,
    funder)` מה-env. ה-creds דטרמיניסטיים למפתח קבוע → תקפים תמיד.
  - **Guardrails חובה:** (1) לעולם אל תאחסן כישלון — רק אחרי הצלחה; (2) rebuild על שינוי מפתח או
    `reset_trading_client_cache()` מפורש; (3) **אל תוסיף שום TTL מעל `get_balance_allowance`** —
    היתרה והקריאות on-chain חייבות להישאר חיות בכל entry/SELL/settlement; (4) `threading.Lock`
    על ה-dict (הקריאה רצה sync מתוך endpoint async).
  - **Latency:** השינוי **מסיר** RTT — היתרה וה-orders נהיים מהירים יותר.
  - **חיסכון:** -95% auth handshakes בזמן מסחר/poll פעיל.

- [ ] **A-7 · `btc-4` — cache קבוע למחירי open/close של settlement** `engine/demo_engine.py:438-452, 1002-1004`
  - **היום:** אותם klines של open/close נמשכים מ-settlement, מ-TP-attach, מלולאת ה-history
    recorder (כל 10s), ומ-`/api/btc/window-prices` — בנפרד. אבל הם **אימ-יוטביליים** ברגע שהנר נסגר.
  - **תיקון:** שים את ה-cache בשתי הפונקציות הנמוכות `fetch_open_price_at_window_start` /
    `fetch_close_price_at_window_end` (לא רק ב-wrapper — כי `auto_history_recorder_loop` קורא להן
    ישירות). מפתח = `(window_epoch_sec, window_sec)`.
  - **Guardrails:** **אחסן רק כשהערך non-None** (close קיים רק אחרי סגירת נר — שמור את בדיקת
    `closeTime>now`); אל תיגע ב-`fetch_btc_spot_usdt`; הגבל/נקה את ה-cache תקופתית (memory).
    זו **לא** תקרית ה-martingale — היא נבעה מ-SETTLE_UNKNOWN שמזין `None`, וכאן אנחנו לעולם לא
    מאחסנים `None`.
  - **חיסכון:** קריאה אחת לחלון במקום 3-4.

---

## 🅱️ קבוצה B — רווח טוב

- [ ] **B-1 · `clob-2` + `clobacct-1` — cache תצוגתי ליתרת ה-CLOB account** `engine/main.py:1673-1676`
  - **היום:** `/api/live/polymarket-clob-account` נמשך כל ~0.8-1.5s ומבצע auth+balance מלא בכל פעם,
    בלי לעבור דרך ה-2s portfolio cache הקיים. היתרה משתנה רק ב-fill/deposit.
  - **תיקון:** הזן את ה-endpoint מתוך ה-payload ש-`fetch_live_portfolio` כבר עושה לו cache 2s
    (`_PORTFOLIO_CACHE`), או cache תצוגתי נפרד 3-5s עם `force` bypass שמתאפס ב-post-trade
    (`reset_portfolio_cache` כבר נקרא ב-`main.py:1566,1600`). אפשר גם פשוט להוריד את ה-endpoint
    מהלולאה החמה ולקחת balance מ-`/api/live/portfolio` (3.5s) הקיים.
  - **Guardrail קריטי:** אל תוסיף cache **בתוך** `fetch_polymarket_clob_account` עצמה —
    `check_balance_before_order` קוראת לה בנתיב ה-entry/exit האמיתי. הוסף test רגרסיה.
  - **חיסכון:** -0.7-0.8 קריאות CLOB/שנייה/טאב.

- [ ] **B-2 · `clob-3` + `imb-1` — `/api/signals`: gather לשני הספרים + cache 1s** `engine/main.py:1715-1732`
  - **היום:** כל קריאה (>1/שנייה בשימוש) מושכת `get_clob_book(token_up)` ואז `(token_down)`
    **סדרתי**, בלי WS-first ובלי cache. המקור היחיד הכי גדול לקריאות `/book` מונעות-UI.
  - **תיקון:** `asyncio.gather` לשני הספרים (חוצה את ה-latency); הוסף cache ברמת ה-handler
    מפתח `(slug, window)` TTL ~1s, שמאחסן את ה-dict המלא.
  - **⚠️ תיקון לדיווח:** אל תזין WS-first לספרי ה-imbalance — `analyze_clob_imbalance` צריך עומק
    מלא (10 רמות), וה-WS שומר רק top-of-book. השאר fetch מלא, או השתמש ב-WS רק ל-`contract_asks`.
  - **Guardrail:** ה-cache רק בתוך ה-handler; `trigger_engine`/`strategy_runner` קוראים `get_clob_book`
    ישירות ולא נוגעים בו.
  - **חיסכון:** -90% קריאות `/book` ב-signals + חצי latency.

- [ ] **B-3 · `clob-4` — single-flight + micro-cache 0.3-0.5s ל-`get_clob_book`** `engine/market_discovery.py:351-370`
  - **היום:** ה-fetcher הקנוני ל-`/book` בלי שום dedup — אותו token יכול להימשך פעמים רבות באותה
    שנייה בין endpoints שונים ובתוך טיק בודד.
  - **תיקון:** עטוף ב-single-flight (`dict[token_id -> Future]`) + micro-cache ≤0.5s, עם פרמטר
    `force=False`. נתיב ביצוע ה-order עובר `force=True` (או נשאר על ה-GET הישיר שלו).
  - **Guardrail:** TTL מקסימום 0.5s; cache רק תוצאות מוצלחות (לעולם לא חריגות); test שמוודא
    ש-`force=True` תמיד פונה חי, ושתי קריאות מקבילות = בקשה אחת.
  - **חיסכון:** מכווץ fetches כפולים של אותו token בין panels ובתוך טיקים.

- [ ] **B-4 · `clob-5` — לקוח keep-alive משותף ל-`fetch_best_bid_ask` fallback** `engine/strategy_runner.py:374-390`
  - **היום:** ה-fallback פותח `httpx.AsyncClient(timeout=6.0)` **חדש בכל קריאה** — בתוך לולאת טיק
    של 0.12s. בזמן נפילת WS זה TLS handshake חדש כמה פעמים בטיק (הנתיב הכי "חם" לקבלת 429).
  - **תיקון:** client singleton ברמת המודול (תשתית 0.2). **בלי result cache** (WS כבר מקדים אותו).
  - **חיסכון:** מבטל handshakes לכל טיק; מוריד churn שמושך rate-limit על נתיב קריטי.

- [ ] **B-5 · `clob-7` — שימוש חוזר בחיבור בלולאות DCA/TP** `engine/trigger_engine.py:506-589, 266-281, 914-934`
  - **היום:** לולאות ה-DCA מושכות 2 ספרים כל 2-3s עם `httpx.AsyncClient` חדש בכל איטרציה, בלי
    WS-first — לאורך כל החלון (דקות).
  - **תיקון:** הרם את ה-client מחוץ ללולאה (תשתית 0.2). נתב את `_fetch_contract_ask` והקריאת bid
    ב-`_check_tp_exits` דרך `fetch_best_bid_ask` (WS-first). השאר fetch מלא לספרי ה-imbalance.
  - **Guardrail:** `best_ask`/`simulate_sell_all` נשארים חיים; שמור את קצב הלולאה (2-3s).
  - **חיסכון:** -70-90% תעבורת `/book` כש-WS מחובר.

- [ ] **B-6 · `btc-3` — לקוח Binance מאוגד + gather ל-open/close** `engine/btc_price.py:182,211,227,263`
  - **תיקון:** `_get_binance_client()` מאוגד (תשתית 0.2) לכל 4 הקריאות; הרץ open+close ב-`asyncio.gather`
    ב-`fetch_window_start_end_btc_usd`.
  - **Guardrail:** **בלי value cache** על מחירי settlement; שמור את לולאת ה-retry של close
    (`closeTime>now`) מילה במילה.
  - **חיסכון:** מסיר handshakes; חוצה את latency ה-settlement.

- [ ] **B-7 · `market-1` — cache per-window ל-`/api/market/current` + הוצאת fetch חוסם** `engine/main.py:871-934`
  - **היום:** נמשך כל ~1s. בחילופי epoch הוא **חוסם עד 3s** על `fetch_open_price_at_window_start`
    בתוך בקשת ה-UI.
  - **תיקון:** cache לשדות הסטטיים מפתח `(epoch, window)`; **תמיד** חשב `seconds_left` חי; העבר את
    fetch ה-open-price ל-`discovery_warmer_loop` ברקע; אם עדיין לא מוכן → החזר `price_to_beat: null`
    (ה-UI סובל null) במקום לחכות.
  - **חיסכון:** מסיר את החסימה של 3s לחלון מנתיב הבקשה.

- [ ] **B-8 · `config-1` — cache לקונפיג סטטי + שימוש חוזר ב-win-rate** `engine/main.py:1330-1397`
  - **תיקון:** cache רק לשדות ה-dataclass הסטטיים, בטל ב-POST config וב-POST mode; ה-win-rate —
    השתמש בערך per-tick מ-A-3 במקום לסרוק שוב 50k.
  - **⚠️ Guardrail:** `loss_recovery_streak`/`loss_recovery_multiplier` (`main.py:1364-1365`)
    **חייבים להישאר דינמיים** — הם מ-`demo.state` ומשתנים ב-settlement; cache עליהם מסתיר martingale בורח.
  - **חיסכון:** -סריקת 50k אחת/שנייה/טאב.

- [ ] **B-9 · `poll-4` — Page Visibility: עצירת טיימרים בהסתר + רענון מיידי בחזרה** `src/api.ts:295-298` + כל ה-pollers
  - **היום:** כל poller בודק `isPageHidden()` ומדלג על ה-fetch, אבל הטיימרים ממשיכים להתעורר 4/שנייה
    בהסתר, ואין `visibilitychange` listener בכלל → בחזרה לטאב המשתמש מחכה עד interval מלא.
  - **תיקון:** subscriber מרכזי אחד ב-`api.ts`: ב-hidden → `clearInterval`; ב-visible → fire מיידי
    אחד ואז restart בקצב המקורי. שמור את ה-`isPageHidden()` guards כ-belt-and-suspenders.
  - **חיסכון:** מסיר את כל ההתעוררויות בהסתר + un-hide מרגיש מיידי במקום עד 10s ישן.

- [ ] **B-10 · `poll-8` — poller יחיד משותף (pub/sub) ל-snapshot + ETag client-side** `src/api.ts:162-277`
  - **היום:** ה-dedup הקיים מכווץ רק בקשות **מקבילות** — polls סדרתיים תמיד מושכים מחדש. App ו-
    LiveStream מריצים את אותו snapshot poll פעמיים.
  - **תיקון:** hook יחיד `setInterval` per-endpoint → fan-out ל-subscribers (מבטל את הכפילות).
    הוסף `If-None-Match`/304 ל-GET תצוגתיים נבחרים; micro-cache ≤300ms whitelisted.
  - **Guardrail:** לעולם לא ל-POST; deny-list מפורש לכל path של order pricing; אופציית `noCache:true`.

- [ ] **B-11 · `disc-3` — endpoint timing קליל ל-UI מתוך ה-peek** `engine/main.py` (חדש) + `market_discovery.py:332`
  - **תיקון:** endpoint שמגיש `slug/epoch/window_sec/seconds_left` מתוך `peek_window_timing_for_ui`
    (בזיכרון, בלי HTTP, בלי lock) — כמו ש-`/api/demo/snapshot` כבר עושה. ה-timer של 250ms פונה אליו.
  - **Guardrail:** אל תחליף את `discover_active_btc_window` ב-endpoints שצריכים tokens/outcome_prices;
    אל תיגע ב-`trigger_engine`/`strategy_runner`.
  - **חיסכון:** מסיר חשיפת Gamma + lock contention מנתיב ה-250ms.

- [ ] **B-12 · `signals-1` — cache דו-שכבתי ל-`compute_signals`** `engine/signal_engine.py:27-87,210-214`
  - **תיקון:** memoize את ה-sub-signals התלויי-לא-ספר (TA/sentiment/history) ~15-20s; חשב את
    ה-CLOB imbalance **חי בכל קריאה**. מפתח לפי `window_sec` (300 מול 900). כבד `force_refresh`.
  - **Guardrail:** אל תיגע ב-entry-price read; TTL ≤20s. (עדיפות נמוכה-בינונית — החיסכון ברשת כבר
    נספג ב-caches הקיימים, זה בעיקר CPU.)

- [ ] **B-13 · `lwo-1` — cache epoch-keyed ל-last-window-outcome + index** `engine/main.py:1929-1996`
  - **היום:** 1-2 שאילתות SQLite ל-`history.db` (4.9MB) **כל שנייה/טאב**, למרות שמשתנה רק בסגירת חלון.
  - **תיקון:** cache מפתח `(epoch, window, flw_params)`; אחד את שתי הקריאות ל-`get_last_window_winners`
    לאחת; בטל ב-POST config. **בנוסף:** הוסף index `window_results(window_sec, epoch DESC)` —
    מבטל את ה-TEMP B-TREE sort ומאיץ גם את נתיב ה-entry החי (רווח כפול, אפס סיכון).
  - **Guardrail:** ה-cache רק ב-handler — לא בתוך `get_last_window_winners` (נתיב ה-FLW entry).

- [ ] **B-14 · `tips-1` — פיצול cache ל-Tips v2** `engine/main.py:605-606, 1462-1505`
  - **היום:** מפתח ה-cache כולל `demo_trades_n` → כל עסקת demo חדשה מבטלת אותו ומכריחה re-parse של
    עד 50 תיקיות ריצה, בכל רענון של טאב ה-Tips בזמן מסחר פעיל.
  - **תיקון:** cache את האגרגט ההיסטורי לפי `(max_runs, min_samples, ...)` + mtime/count של תיקיות
    הריצה; מזג live trades per-request (כך שעסקאות המשתמש מופיעות תוך רענון). הוצא את `demo_trades_n`
    מהמפתח של האגרגט.
  - **חיסכון:** עצירת re-parse כבד בכל רענון → חישוב כבד אחד ל-5 דקות.

---

## 🅲 קבוצה C — ליטוש (effort נמוך, רווח קטן-בינוני)

- [ ] **C-1 · `ob-1` — ETag/304 ל-`/api/market/orderbook-summary`** `engine/main.py:984-1086` — כבר WS+TTL cached; רק להוסיף ETag (כלול את דגלי `source`/`degraded` ב-hash).
- [ ] **C-2 · `contractprice-1` — ETag/304 ל-`/api/contract-prices`** `engine/main.py:1770-1831` — ETag על ערכי ה-payload בלבד; **אל תאט** את ה-750ms (זה ticker חי) ואל תרחיב את ה-0.5s TTL; טפל ב-304 כ-poll מוצלח כדי לא להפעיל את מחוון ה-staleness.
- [ ] **C-3 · `livemode-1` — ETag/304 ל-`/api/live/mode`** `engine/main.py:1615-1650` — ETag על ה-payload החי; **בלי** body cache בטיימר (אחרת flip של kill-switch מוסתר).
- [ ] **C-4 · `logs-1` — incremental/cursor ל-logs + log-entries** `engine/main.py:1442-1450` — הוסף `_log_seq` מונוטוני ב-`strategy_runner`, `?since=<seq>` מחזיר רק חדשים; אפס seq ב-mode change.
- [ ] **C-5 · `analytics-1` — cache + memoize ל-`/api/analytics/*`** `engine/analytics/api_routes.py:67-301` — TTL קצר (≤5s) או fingerprint; memoize `compute_global_metrics` בתוך `/full-report`; דלג על `ensure_analytics_tables()` ב-cache hit.
- [ ] **C-6 · `btc-5` — לקוח Polygon מאוגד ל-Chainlink fallback + guard** `engine/btc_price.py:168-195` — נתב `..._latest` דרך `_polygon_eth_call` המאוגד; early-return ב-`_upgrade_to_chainlink` אם ה-epoch כבר ב-cache (מונע עד 200 קריאות getRoundData).
- [ ] **C-7 · `btc-7` — (אופציונלי) WS push ל-`/api/btc/live`** `engine/main.py:937-944` — דחיפת מחיר BTC ב-WS במקום poll 800ms/לקוח. **⚠️ אל תעלה את `_BTC_SPOT_CACHE_TTL_SEC` מעל 1s** (momentum entry); הוסף startup assert. (אין WS spot כיום — `ws_price_stream` הוא רק CLOB.)
- [ ] **C-8 · `disc-4` — thread תוצאת discovery אחת לטיק** `engine/trigger_engine.py:194-956` — resolve `ActiveMarket` פעם בראש `_tick` והעבר אותה לכל ה-helpers (רק זיהויים, **לא** מחיר). משפר עקביות epoch בטיק. (תלוי ב-A-5.)
- [ ] **C-9 · `poll-6` — poller משותף ל-`/api/trigger/state`** `src/TriggerTrader.tsx:347-353`, `src/LiveStreamTrade.tsx:946-960` — hook pub/sub יחיד; השאר 2000ms. (⚠️ ה-poll **לא** דורס עריכות config — ההנחה בדיווח שגויה, אל "תתקן" באג שלא קיים.)
- [ ] **C-10 · `poll-7` — הוסף `isPageHidden` ל-FaultsTab** `src/FaultsTab.tsx:103-107` — ה-poller היחיד בלי gate; עטוף + הוסף `visibilitychange` רענון מיידי.
- [ ] **C-11 · `fng-1` — Fear&Greed TTL 1h→6h (או לפי `time_until_update`)** `engine/sentiment.py:21-23` — ה-API מחזיר `time_until_update`; clamp ל-[1h, 8h]. הוסף הערה ש-F&G הוא נתון יומי.
- [ ] **C-12 · `funding-1` — Funding rate TTL 5min→30min** `engine/sentiment.py:18-19` — מיושר למחזור 8h של funding. הוסף הערה.

---

## ⚠️ קבוצה D — דורש זהירות (הגרסה הנאיבית מסוכנת!)

> שני אלה אומתו כ**לא בטוחים כפי שנוסחו במקור**. האימות-היריב תפס בהם מלכודת. בצע רק לפי הגרסה
> המתוקנת למטה.

- [ ] **D-1 · `state-1` — לפצל את `mark_to_market` + כתיבת 6MB מתוך הבקשה** `engine/main.py:1089-1100`
  - **הבעיה:** `/api/demo/state` (כל ~1s) מריץ `mark_to_market()` (קריאת CLOB סינכרונית per-position
    ב-throttle miss) + יכול לכתוב את `demo_state.json` (6MB) לדיסק בתוך בקשת ה-GET.
  - **🚨 למה הגרסה הנאיבית מסוכנת:** **אין לולאת mark ברקע.** `mark_to_market` נקרא רק מ-3 מקומות,
    ו-`_tick` עושה early-return כש-`mode=='off'` — וזה ברירת המחדל אחרי **כל** restart. כש-המנוע
    OFF, ה-endpoint הזה הוא ה**יחיד** שמקדם את `last_mark` ואת ה-TP-settlement-backfill. הסרה
    נאיבית תקפיא את ה-PnL **ותעצור את backfill ה-settlement** — בדיוק סוג ה-plumbing שגרם לתקרית ה-85%.
  - **תיקון בטוח (לפי סדר!):**
    1. בנה קודם את לולאת הרקע (תשתית 0.4) שמריצה `mark_to_market` + backfill + save **ללא תלות במצב**.
    2. **אמת** שהיא מקדמת `last_mark.ts` כשהמנוע OFF.
    3. **רק אז** הפוך את `/api/demo/state` לקריאת `last_mark` ישירה + ETag/304, והוצא את ה-save מנתיב ה-GET.
  - **Guardrail:** entry/exit ממשיכים לקרוא `fetch_best_bid_ask` חי; שמור את gate ה-WS של 30s.

- [ ] **D-2 · `klines-1` — להפחית את קצב משיכת ה-klines** `engine/ta_signals.py:16-47,102`
  - **הבעיה:** TTL 15s מושך את אותו נר 1m ~4 פעמים/דקה; 59 מ-60 הנרות אימ-יוטביליים.
  - **🚨 למה היישור-לגבול-נר מסוכן:** ה-open bar החי נושא את `closes[-1]` = המקור ל-`momentum_3m/5m`.
    יישור ל-boundary יקפיא אותו עד ~60s. ה-momentum מתהפך ב-±0.05%, ו-BTC זז יותר מזה בתוך דקה →
    כיוון entry שגוי כמעט דקה שלמה. זו **לא** תקרית ה-martingale, אבל זה כן input להחלטה.
  - **תיקון בטוח:** **אל תקפיא את ה-open bar.** הפשוט ביותר: הגבל TTL ל-**10s** (אף פעם לא
    `60-(now%60)`). הטוב ביותר: משוך היסטוריה סגורה (`limit=59`) ב-TTL לגבול-נר, והרכב את הנר החי
    מ-`fetch_btc_spot_usdt` (1s) → ~1 משיכת היסטוריה/נר + momentum sub-second.
  - **Guardrail:** אל תנתב `_fetch_contract_ask`/`_fetch_btc_price` דרך cache של klines.

---

## 🗺️ סדר ביצוע מומלץ

```
שבוע 1 — תשתית + Quick wins ב-Frontend (סיכון אפס, רווח מיידי):
  0.1 cache util  →  0.2 לקוחות keep-alive  →  0.3 ETag helper
  A-1, A-2 (snapshot 250→1000ms)  ·  B-9 (Page Visibility)  ·  C-10, C-11, C-12

שבוע 2 — שרת hot-path (הרווח הגדול ב-CPU):
  A-3 (snapshot cache + win-rate once)  ·  A-4 (tiered bundle)  ·  B-10 (shared poller)
  B-8, B-13 (config + history caches + index)  ·  C-1..C-4 (ETag לכל ה-GET)

שבוע 3 — תעבורת חוץ (Polymarket/Binance):
  A-5 (discovery window cache)  ·  A-6 (CLOB client memoize)  ·  A-7 (settlement price cache)
  B-1, B-2, B-3, B-6 (account/signals/book/binance)

שבוע 4 — נתיבי מסחר (connection reuse, sub-second) + זהירות:
  B-4, B-5 (keep-alive ב-tick/DCA)  ·  B-7, B-11, B-12, B-14  ·  C-5..C-9
  0.4 (background mark loop)  →  D-1  ·  D-2

עצמאי לחלוטין (אפשר מתי שרוצים): C-11, C-12, C-10, B-13's index
```

**עיקרון:** קבוצה A לבד מורידה ~70% מהעומס. אם יש זמן מוגבל — עשה רק את שלב 0 + קבוצה A.

---

## ✅ החלטות שקיבלתי בשמך (ענית לי שאלות לפני שהלכת לישון)

1. **קצב ה-snapshot:** עודכן ל-**500ms** (לא 1000ms) — אחרי ששאלת על האחוז החי בחלון העסקה.
   500ms משמר את חלקות האחוז (שמתעדכן מ-`last_mark` דרך ה-snapshot) וגם חוצה את התעבורה.
   הרווח האמיתי בא מ-A-3 (cache+ETag), שבלתי נראה לך. ה-ticker של BTC נשאר ≤750ms דרך WebSocket.
2. **תשתית קודם:** עדיף לבנות את 4 רכיבי התשתית (שלב 0) לפני ה-caches הבודדים — חוסך כפילות קוד.
3. **`state-1` (D-1):** לא להסיר את ה-mark מה-handler "כמו שהציעו" — חייבים קודם לולאת רקע, אחרת
   ה-PnL וה-settlement backfill נתקעים כשהמנוע OFF (וזה נדלק OFF בכל restart).
4. **`klines-1` (D-2):** הגבלת TTL ל-10s, **לא** יישור לגבול-נר (היה מקפיא את ה-momentum עד דקה).
5. **כל ה-caching בשכבת תצוגה בלבד:** אף נתיב מחיר/יתרה/settlement של order לא מקבל cache —
   ראה טבלת ה-Guardrails הקדושים למעלה.
6. **F&G = 6h, Funding = 30min:** מיושר לקצב העדכון האמיתי שלהם (יומי / 8 שעות).
7. **מיקום הקובץ:** `API_RESOURCE_TASKS.md` בשורש, בהתאם למוסכמה של `QA_TASKS.md` / `TASKS.md`.

---

## 📎 נספח — מקורות ה-API החיצוניים שמופו

| Host | שימוש | קבצים |
|---|---|---|
| `clob.polymarket.com` | order book, מחירים, יתרה, orders | `live_clob.py`, `market_discovery.py`, `strategy_runner.py`, `trigger_engine.py` |
| `gamma-api.polymarket.com` | גילוי שווקים (slug/tokens) | `market_discovery.py` |
| `data-api.polymarket.com` | נתוני שוק | `market_discovery.py` |
| `api.binance.com` | מחיר BTC spot + klines | `btc_price.py`, `ta_signals.py` |
| `fapi.binance.com` | funding rate | `sentiment.py` |
| `api.alternative.me` | Fear & Greed index | `sentiment.py` |
| `polygon.drpc.org` / `1rpc.io` / `publicnode.com` | Chainlink price-to-beat (RPC) | `btc_price.py` |

**Caching קיים לפני הביקורת:** `_PORTFOLIO_CACHE` (2s), `_contract_price_cache` (0.5s),
`ORDERBOOK_SUMMARY_CACHE` (0.5s), `_DISCOVERY_CACHE` (30s), `_CLOB_MIN_SIZE_CACHE` (120s),
`_BTC_SPOT_CACHE` (1s), `_CHAINLINK_AT_WINDOW_CACHE` (per-epoch), `tips_v2_cache.json` (5min),
klines (15s), funding (5min), F&G (1h), in-flight GET dedup ב-`api.ts`.

---

*נוצר ע״י ביקורת רב-סוכנית: 7 מנתחים במקביל × אימות יריב לכל ממצא × סינתזה.
44 ממצאים → 37 אומתו → 35 בטוחים + 2 בזהירות. כל ה-file:line מאומתים מול הקוד.*
