# QA Audit — Polymarket Bot v3 (after session improvements)

**Date:** 2026-05-27
**Scanned:** `engine/main.py`, `engine/strategy_runner.py`, `engine/demo_engine.py`, `engine/live_clob.py`, `engine/history_tracker.py`, `engine/btc_price.py`, `engine/order_validation.py`, `engine/secret_store.py`, `engine/loss_recovery.py`, `engine/request_logger.py`, `engine/analytics/db_migration.py`, `src/App.tsx`, `src/hooks/usePriceStream.ts`

---

## Critical (must fix before live-money trading)

- [ ] **#1 — Zero authentication on write endpoints + CORS=`*` → anyone on the internet can drain your account**
  `engine/main.py:760-766` (CORS) + `engine/main.py:1473,1511,1604,1190,1321` (open POSTs)
  - מה קורה: `app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)`. ה-bot ב-Railway ב-`https://polymarket-bot-mac-v3-production.up.railway.app`. **אין שום auth** על:
    - `POST /api/live/private-key` — שמירת מפתח פרטי
    - `DELETE /api/live/private-key` — מחיקת מפתח
    - `POST /api/live/order` — ביצוע הזמנה אמיתית בכסף
    - `POST /api/strategy/config` — שינוי הגדרות
    - `POST /api/strategy/mode` — הדלקת מצב אוטו/לייב
  - איך זה ינוצל: כל אתר/אדם שיודע את ה-URL שולח `curl -X POST https://.../api/live/order -d '{"side":"Up","token_id":"<malicious>","contracts":1000,"limit_price":0.99}'` והבוט (אם live mode דולק) יחתום וישלח. או — `POST /api/live/private-key` עם מפתח של תוקף → הבוט יסחור לארנק התוקף.
  - תיקון: אחת מהאופציות:
    1. **API token סודי** — ENV `BOT_API_TOKEN`, נדרש header `X-Bot-Token` בכל write. Frontend מקבל אותו דרך build var.
    2. **Cloudflare Tunnel + Access** — מוסיף שכבת אימות לפני שמגיעים ל-Railway.
    3. **IP allowlist** — רק מהמכשירים שלך.
    4. **לפחות:** להוריד את ה-domain הציבורי ב-Railway אם לא נחוץ; להריץ מקומי + tunnel.
  - מה דחוף לעשות עכשיו: לא להפעיל live mode בייצור כל עוד אין auth.

- [ ] **#2 — `_rollover_lock` (FIX #24) לא מגן על העבודה האמיתית של ה-rollover, רק על ה-double-check**
  `engine/strategy_runner.py:1003-1010`
  - מה קורה: ה-`async with self._rollover_lock:` block מסתיים ב-line 1009 אחרי `return`. כל קוד ה-rollover (settlement, LR, history write) נמצא **אחרי** הסגירה של ה-with, ולכן לא מוגן.
  - הוכחה: Tick A מקבל את ה-lock, עושה double-check, יוצא מה-with, מתחיל את ה-rollover. בזמן ש-A ב-await של `expire_all_outside_tokens`, Tick B מגיע, מקבל את ה-lock (A כבר שחרר), double-check עובר (כי current_epoch עוד לא עודכן ל-m.epoch — זה קורה רק בשורה 1153), יוצא מה-with ומתחיל settlement במקביל ל-A.
  - תוצאה: כפילות settlement_trades, כפילות apply_loss_recovery → ה-multiplier יכול לקפוץ פעמיים בפועל למרות הגנת ה-rollover_lock על הנייר.
  - תיקון אופציה A: להכניס את כל הרולאובר לתוך ה-with:
    ```python
    if m.epoch != self.rt.current_epoch:
        async with self._rollover_lock:
            if m.epoch == self.rt.current_epoch:
                return
            # ... כל הקוד עד current_epoch = m.epoch ...
    ```
  - תיקון אופציה B (פשוט יותר): לעדכן `current_epoch = m.epoch` בתוך ה-with כסנטינל מיד, ואחרי שחרור ה-lock לעשות את העבודה. הסנטינל מבטיח שטיק שני לא נכנס.

- [ ] **#3 — `/api/live/order` לא מוודא טווח מחיר (0.01-0.99)**
  `engine/main.py:1604-1621`
  - מה קורה: `price = float(body.limit_price or 0.5)`. אין clamp ל-[MIN_LEGIT_SHARE_PRICE_USD, MAX_LEGIT_SHARE_PRICE_USD]. אם מישהו שולח `limit_price=10.0` → קנייה ב-$10 לחוזה. אם שולח 0 → קנייה חינם (פולימרקט ידחה אבל זה מבזבז API). שילוב עם #1 = catastrophic.
  - תיקון: לפני קריאה ל-live_place_entry_order, להוסיף `if not MIN_LEGIT_SHARE_PRICE_USD <= price <= MAX_LEGIT_SHARE_PRICE_USD: raise HTTPException(400, ...)`.

---

## High

- [ ] **#4 — `auto_history_recorder_loop` שולל `min_btc_drift_pct` filter / records side_won=None לא נספרים, ועלולים להישאר כך לנצח**
  `engine/main.py:486-523`
  - מה קורה: אם `fetch_close_price_at_window_end` מחזיר None (kline lag הרגיל ביותר ב-1-2s האחרונים), ה-recorder לא רושם — אבל הוא **כן** מקדם את prev_epoch ל-current_epoch בסיבוב הבא. אם prev_epoch לא נרשם, לא ינסה שוב — הוא אבוד מהיסטוריה.
  - הוכחה: בדוק את ה-flow ב-486-523 — בשורה 488 יש check ל-`current_epoch != prev_epoch and prev_epoch != _last_recorded_epoch`. אם הפעם הראשונה נכשלה (kline לא היה), אנחנו עושים `continue` בלי לעדכן _last_recorded_epoch, אבל גם בלי לזכור את ה-epoch לרישום מאוחר יותר. בסיבוב הבא, prev_epoch כבר התחלף ל-current_epoch של הסיבוב הקודם. ה-epoch שנכשל יילך לאיבוד.
  - מתי זה רלוונטי: Binance lag מעל 10s (זמן ה-recorder loop) או startup mid-window.
  - תיקון: לתחזק תור של pending_epochs לרישום, ולנסות שוב בכל סיבוב עד שמצליחים. או: backfill task ב-#5 שלי כיסה את החלונות הראשונים — הוא רץ פעם אחת ב-startup, לא ממשיך.

- [ ] **#5 — `request_logger` רץ על כל בקשה ועלול לרשום body של POST /api/live/private-key**
  `engine/request_logger.py:83-110`
  - מה קורה: צריך לבדוק האם middleware רושם body. אם כן — המפתח הפרטי שלך מופיע ב-logs/requests.jsonl. ה-volume מתמשך → המפתח על דיסק בלי הצפנה.
  - תיקון: לוודא ש-init_request_logger לא רושם body, או להוסיף path blacklist (`/api/live/private-key`, `/api/live/order`) שמחליפים body ב-`<redacted>`.
  - מעקב: לקרוא את request_logger.py:83-110 ולדווח.

- [ ] **#6 — `history_tracker.py:65` עדיין משתמש ב-`utcfromtimestamp` (deprecated) ב-fallback**
  `engine/history_tracker.py:64-66` + `engine/analytics/db_migration.py:255`
  - מה קורה: ניסיתי לתקן ב-#18 שלי עם try/except, אבל ה-fallback עדיין משתמש ב-deprecated API. Python 3.13 כבר מתריע; 3.15+ אולי יסיר.
  - תיקון: להחליף ב-`datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)` ב-fallback גם.

- [ ] **#7 — 177+ `except Exception:` ריקים בקוד — שגיאות נעלמות בלי לוג**
  `engine/main.py` (15+ instances), `engine/strategy_runner.py`, `engine/demo_engine.py`, `engine/live_clob.py`
  - מה קורה: רוב ה-handler-ים פשוט `pass` או `continue` בלי לרשום מה קרה. כשאתה בא לדבג למה הבוט עשה משהו לא הגיוני, אתה לא יכול לראות את השגיאה המקורית.
  - מתי קריטי: שגיאה ב-live_clob.place_entry_order נבלעת → הבוט "חושב" שהזמנה עברה אבל בפועל לא. או: שגיאה ב-`_record_settlement_to_history` נבלעת → history.db לא מתעדכן, FLW מקבל נתון ישן.
  - תיקון מינימלי: בכל מקום `except Exception` קריטי → `except Exception as e: self.rt.log(f"<context>: {e!r}")`. מדובר ב-~50 מקומות מתוך 177 שהם באמת קריטיים.

- [ ] **#8 — `max_trades_per_hour` ברירת מחדל 1000 + `max_entries_per_window` 3 — מאפשר 1000 כניסות בשעה אחת**
  `engine/main.py:1067-1068`
  - מה קורה: עם FLW + LR martingale, אם רצף הפסדים ארוך, הבוט יכול לעלות במכפיל ב-multiplier=100×. עם base $5 + mult 100 → $500 לעסקה. אם זה רץ 1000 פעמים בשעה — $500,000 חשיפה.
  - תיקון: להוריד את ברירת המחדל ל-30 trades/hour. או לכפות תקרת notional/hour חכמה במקום למספר.

- [ ] **#9 — אין rate limit על endpoints ציבוריים → DoS קל**
  `engine/main.py` (כל הroutes ללא middleware)
  - מה קורה: אין `slowapi` או דומה. תוקף יכול לשלוח 1000 בקשות/שניה ולהפיל את uvicorn.
  - תיקון: להוסיף `slowapi` עם limit סביר (60 req/min לכל IP). פחות דחוף מ-#1 (auth), אבל משלים.

---

## Medium

- [ ] **#10 — `_persist_dca_counters` לא שומר חלק מהשדות לפני שגיאת save**
  `engine/strategy_runner.py:374-381` (אחרי תיקון #22)
  - מה קורה: בתוך הפונקציה — קודם מעדכן את ה-state עם הערכים, ואז קורא ל-`self.demo.save()`. אם save נכשל, ה-state בזיכרון השתנה אבל לא ב-disk. בקריאה הבאה (אחרי restart) ה-state יחזיר את הערכים הישנים — disagreement עם המציאות.
  - תיקון: לעטוף ב-try; אם save נכשל, להחזיר את ה-state לערכים הישנים.

- [ ] **#11 — `_record_settlement_to_history` לא מטפל ב-mismatch של slug/epoch**
  `engine/strategy_runner.py:_record_settlement_to_history`
  - מה קורה: הקוד מנסה לקבוע את ה-slug של החלון הישן מתוך ה-trade. אם ה-trade לא מכיל `slug` (וזה רוב המקרים — לא ראיתי שמסומן ב-expire_all_outside_tokens), הוא ירכיב את ה-slug מהתבנית. אם התבנית לא מתאימה (`btc-updown-5m-` vs `btc-updown-15m-`), ה-record יכתב עם slug שגוי, וב-`get_last_window_winners` הסינון לפי window_sec עדיין יעבוד, אבל היסטוריית הרישומים תהיה לא־עקבית בלוגים.
  - תיקון: לוודא ש-expire_all_outside_tokens מסמן slug; או לפענח את החלון מ-token_id (פולימרקט slug יודע).

- [ ] **#12 — `_settled_token_ids` הוא set שלא נמחק לעולם (memory leak איטי)**
  `engine/strategy_runner.py:150-151`
  - מה קורה: כל token_id שמטופל ב-expire נכנס ל-set. יש cleanup ב-line 884 ("ניקוי settled_token_ids אחרי שעה") שמתבצע רק אם `time.time() - self.rt._settled_token_ids_ts > 3600`. אבל ה-`_settled_token_ids_ts` מתעדכן בכל update, אז אם כל 5 דקות מתווסף token חדש, ה-clear() **לעולם לא** רץ. ב-7 ימים → 2016 חלונות × 2 tokens = ~4000 token_ids ב-set.
  - תיקון: לשנות את הקריטריון ל-"clear after total set size > 500" או "clear after 1h since LAST clear", לא "since last update".

- [ ] **#13 — `_position_tracking.pop(token_id)` ב-`expire_all_outside_tokens` ולא נשמר לדיסק**
  `engine/demo_engine.py:454, 627`
  - מה קורה: ה-`_position_tracking` dict שומר את ה-peak/trough watermarks של כל פוזיציה. הוא רק בזיכרון, לא בדיסק. אם הבוט קורס באמצע חלון — ה-peak/trough של פוזיציות פתוחות נעלם, וה-trade שייסגר במצב next-start יחסר נתונים.
  - תיקון: להוסיף `_position_tracking` ל-`DemoState.to_dict / from_dict`. הוא קצת גדול (path[] עד 5000 דגימות לכל אחד) — אפשר לדדם את ה-path בזיכרון בלבד ולהשאיר רק peak/trough בדיסק.

- [ ] **#14 — `record_window_result` שולח 9 פרמטרים — קל לבלבל בקריאות עתידיות**
  `engine/history_tracker.py:40-90`
  - מה קורה: פונקציה עם 7 פרמטרים אופציונליים. ב-`auto_history_recorder_loop` ו-`_record_settlement_to_history` שניהם קוראים. אם פעם מישהו יבלבל בסדר (כי כולם float-ים), זה יכשל בשקט (SQLite מקבל כל סוג).
  - תיקון: לעטוף ב-dataclass `WindowResult` עם named fields.

- [ ] **#15 — `init_request_logger` רושם POST body של `_log/client-request` (recursive)**
  `engine/request_logger.py:83+`
  - מה קורה: ה-frontend שולח `POST /api/_log/client-request` עם תוכן ה-log. ה-request_logger רושם את ה-request הזה — אז כל log של frontend נכפל. ה-DB גדל מהר.
  - תיקון: לרשום skip ל-`/api/_log/client-request` ב-init_request_logger.

---

## Low

- [ ] **#16 — frontend `usePriceStream` אין URL escaping ל-`window.location.port`**
  `src/hooks/usePriceStream.ts:33`
  - מה קורה: `if (port === "5175") return ...`. אם הפרויקט יעבור פורט (5176 וכו'), צריך לשנות ידנית. לא בעיה אבל קוד שביר.
  - תיקון: להוציא ל-env var.

- [ ] **#17 — `TradeBody.side` field לא בשימוש ב-`/api/live/order`**
  `engine/main.py:1116-1121, 1604-1621`
  - מה קורה: `side` ב-body לא משמש ב-live_order — ה-side נקבע מ-token_id. שדה מבלבל.
  - תיקון: או להסיר את `side` מ-TradeBody או להשתמש בו לוולידציה כפולה (side צריך להתאים ל-token_id).

- [ ] **#18 — `loss_recovery` log message ב-Hebrew בלבד**
  `engine/loss_recovery.py:52,64,70-75`
  - מה קורה: כל המסרים בעברית. אם פעם תרצה לתמוך באנגלית או לראות לוגים אצל Anthropic/Sentry — קשה.
  - תיקון: i18n או לפחות לוג קצר באנגלית במקביל. לא דחוף.

- [ ] **#19 — `engine/atomic_io.py` לא מטפל ב-symlink**
  `engine/atomic_io.py:atomic_write_text`
  - מה קורה: כותב tmp בתוך path.parent ואז os.replace. אם path הוא symlink — היעד הסופי משוכפל בנכון, אבל הוא עוקף את ה-symlink (כותב לטרגט, לא לקישור). נדיר ב-volume של Railway.
  - תיקון: לא קריטי.

- [ ] **#20 — Frontend bundle 929KB (אזהרה ב-vite build)**
  `dist/assets/index-*.js`
  - מה קורה: bundle גדול → טעינה ראשונית איטית. recharts ו-תלויות UI אחרות יוצרות bloat.
  - תיקון: code splitting עם React.lazy לרוט TabsTipsV2/AnalyticsV3 (תכונות שלא בשימוש בכניסה ראשונה).

---

## Summary

- **3 critical** (must fix before any live-money trading on the public URL).
- **6 high** (real bugs / brittleness affecting workflow).
- **6 medium** (improvements that won't break things in the short term).
- **5 low** (polish).

### השלוש הקריטיים בקצרה
1. **אין authentication** + CORS=`*` → כל בעולם יכול לשלוח orders ולעיין/לשנות הגדרות. **לא להפעיל live trading עד שזה מתוקן.**
2. **ה-rollover_lock לא עובד באמת** — מגן רק על double-check, לא על העבודה. סיכון לכפילות LR multiplier.
3. **/api/live/order** לא מוודא טווח מחיר → תוקף יכול לכפות price=10 (אם פולימרקט יקבל מאיזשהי סיבה).

### למה צריך לתקן את הקריטיים
- אתה רוצה להפעיל live trading (kelly martingale עם כסף אמיתי).
- ה-bot URL חשוף לאינטרנט.
- בלי auth, מישהו שמוצא את ה-URL (יש אינדקסים, מנועי חיפוש, scanners) יכול לעשות נזק תוך 30 שניות.
- ה-rollover lock bug יכול לכפיל את ה-multiplier מאחורי הקלעים — multiplier 2.5× יהפוך פתאום ל-6.25× בלי שעבר הפסד.

### למה לדחות את ה-low
- bundle size לא משפיע על trading.
- symlink בעיה תיאורטית.
- i18n של logs לא משפיע על מסחר.

---

*Next: User יבחר אילו ממצאים לתקן.  
Recommended order: **#1 first** (auth + CORS) → **#2** (lock) → **#3** (price clamp) → אז #4-#9 שדורש פחות.*
