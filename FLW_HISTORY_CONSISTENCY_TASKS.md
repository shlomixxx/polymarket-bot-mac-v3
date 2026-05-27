# FLW + History Consistency — Task List

**Date:** 2026-05-27
**Root Cause:** מקור־האמת על "מי ניצח בחלון" אינו אחיד בין שני נתיבים בקוד.
- `expire_all_outside_tokens` (demo_engine) משתמש ב-`fetch_close_price_at_window_end` (kline 1m close) ובכלל `>=` ל-tie.
- `auto_history_recorder_loop` (main) משתמש ב-`fetch_btc_spot_usdt` (spot חי בזמן הריצה, עד ~10s אחרי סוף החלון) ובכלל `>` ל-tie.

תוצאה: ה-`history.db` (שממנו FLW קורא) יכול לקבוע "Down ניצח" בעוד הבוט עצמו סגר את הפוזיציה כ"Up ניצח" — או להפך. FLW אז מכוון את הכניסה הבאה ל**צד הלא־נכון**, וה-loss recovery מכפיל באותו כיוון לא־נכון → רצף הפסדים מבני (Martingale שמכפיל נגד הכיוון).

הוכחה חיה מהפרודקשן:
```
window epoch=1779845100 (01:25 UTC):
  bot's settlement:      resolved=Up  → user $124 loss on Down position
  history.db:            side_won=Down  (btc 75864 → 75840)
  ⚠ disagreement on the SAME window
```

---

## Findings

### Critical

- [ ] **#1 — שני מקורות אמת שונים ל-window winner**
  `engine/main.py:489-494` (auto_history_recorder_loop) vs `engine/demo_engine.py:482-485` (expire_all_outside_tokens)
  - מה קורה:
    - recorder: `btc_close = fetch_btc_spot_usdt()` ← מחיר ספוט עכשיו, לא סגירת חלון
    - settlement: `end_p = fetch_close_price_at_window_end(ep, ws)` ← kline-based, סגירה אמיתית של החלון
    - מחירים שונים → תוצאות שונות → FLW מקבל החלטה הפוכה מהמציאות שהבוט חי בה.
  - השפעה על המשתמש: אחרי הפסד, FLW מצביע על הצד **הלא־נכון** (לפי המציאות של הבוט), והכפלת loss_recovery (2.5×, 6.25× ...) מופעלת באותו כיוון שגוי. מתועד בפרודקשן: בעסקה #5 שמופיעה ב-trades sequence, היה הפסד $124 כי FLW כיוון ל-Down בזמן שמבחינת הבוט נסגר ב-Up.
  - תיקון: `auto_history_recorder_loop` חייב לקרוא ל-`fetch_close_price_at_window_end(prev_epoch, prev_window_sec)` במקום `fetch_btc_spot_usdt()`. זה מבטיח שאותו נתון משמש את שני הנתיבים.

- [ ] **#2 — tie-handling לא־עקבי בין settlement ל-recorder**
  `engine/demo_engine.py:482` (`>=`) vs `engine/main.py:494` (`>`)
  - מה קורה:
    - settlement: `resolved_up = float(end_p) >= float(start_p)` → תיקו ⇒ Up
    - recorder:   `side_won = "Up" if btc_close > btc_open else "Down"` → תיקו ⇒ Down
  - השפעה: בחלון שבו BTC לא זז (או נע <0.01$), שני המקורות יתנו תשובות הפוכות. FLW יבחר הפוך מהמציאות.
  - תיקון: יישור recorder ל-`>=` (תואם את כלל ה-bot ולמה שמוצג ב-Dashboard: "Up מנצח אם BTC אינו נמוך"). שינוי שורה: `side_won = "Up" if btc_close >= btc_open else "Down"`.

- [ ] **#3 — race condition: history.db לא מתעדכן בזמן rollover**
  `engine/strategy_runner.py:1402-1405` (FLW gating in _tick) + `engine/main.py:466` (recorder runs every 10s)
  - מה קורה:
    - strategy_runner._tick רץ כל ~2s.
    - auto_history_recorder_loop רץ כל 10s. כשהוא מזהה rollover, הוא רושם את החלון הקודם.
    - לכן בין rollover-detection ב-_tick לבין רישום ה-recorder יכולים לעבור עד 10s שבהם `history.db` לא מכיל את החלון האחרון. FLW יקבל את החלון הקודם־קודם.
  - השפעה: `min_minutes_for_entry=3` מצמצם את הסיכון בקונפיג הנוכחי כי הבוט מחכה 3 דק׳ לפני כניסה. אבל אם המשתמש יוריד את הערך הזה → הסיכון פעיל.
  - תיקון: strategy_runner שיכתוב ל-`record_window_result()` מיד אחרי `expire_all_outside_tokens` (בתוך הבלוק של rollover, line ~840-860). שני הנתיבים יתאמו לאחר התיקון הזה.

### High

- [ ] **#4 — `fetch_close_price_at_window_end` יכול להחזיר None ברגע סגירת חלון**
  `engine/btc_price.py:240-261`
  - מה קורה: כשהבוט מזהה rollover ב-_tick ב-t=window_end+δ (δ < 1-2s), Binance עוד לא בהכרח פרסם את הנר שזה עתה נסגר. הקריאה מחזירה data=[] → None.
  - השפעה: `expire_all_outside_tokens` נופל ל-`btc_prices_unavailable` ב-line 472-480 → trade["type"]="SETTLE_UNKNOWN" → realized_pnl=-leg_cost (הפסד שקרי גם אם הצד היה אמור לנצח). יש כבר fallback ב-`_backfill_missing_tp_settlement_btc` (line 945) ל-TP, אבל לא ל-SETTLE_UNKNOWN של פירוק חלון.
  - תיקון: ב-`fetch_close_price_at_window_end` להוסיף retry קצר (1-2 שניות) אם data ריק. אופציה משלימה: backfill ל-SETTLE_UNKNOWN בדומה לקיים ל-SELL_TP.

- [ ] **#5 — recorder לא מתעדכן אם הבוט כבוי או נופל**
  `engine/main.py:448-522`
  - מה קורה: recorder תלוי ב-uvicorn process. אם השרת נופל בתוך חלון או לא רץ כשחלון מסתיים — אותו חלון לעולם לא יירשם.
  - השפעה: FLW מקבל נתון "חסר" באמצע הרצף, מה שיכול להוביל ל-tie או ל-fallback. בקונפיג הנוכחי lookback=1, היעדר חלון יחיד עלול להעלים את ההחלטה.
  - תיקון: backfill ב-startup — כשהשרת עולה, לבדוק את N החלונות האחרונים שצריכים להיות ב-history.db אבל חסרים, ולמלא אותם דרך binance klines (יותר מ-recorder הרגיל כי קוראים שעות אחורה).

- [ ] **#6 — recorder מתעלם מ-window_sec**
  `engine/main.py:468` (`discover_active_btc_window()` ללא ארגומנט) + `engine/main.py:475` (`current_window_sec = m.window_sec`)
  - מה קורה: ה-recorder תופס את ה-window_sec מהשוק הנוכחי שמתגלה (ברירת מחדל 5m), ומשתמש בו לכל החלונות. אם המשתמש עובר בין 5m ל-15m, יש tracking לא־עקבי.
  - השפעה: רלוונטי רק במקרי קצה (משתמש שמשנה window_sec בזמן ריצה).
  - תיקון: לוודא ש-discover_active_btc_window נקרא עם cfg.btc_window הנכון. כרגע משאיר ל-fallback של הפונקציה.

### Medium

- [ ] **#7 — FLW לא בוחן אם נתון חסר מ-history.db**
  `engine/strategy_runner.py:785-794`
  - מה קורה: `get_last_window_winners(limit=lookback)` מחזיר עד lookback חלונות. אם יש 0 חלונות → fallback. אם יש פחות מ-lookback (למשל lookback=3 אבל ב-DB יש רק 2) → לוקח את שני שהוא רואה ולא מתריע.
  - השפעה: עם lookback=1 (הקונפיג הנוכחי) — לא רלוונטי. עם lookback>1 — החלטה על בסיס פחות נתונים מהמתוכנן, ייתכן רוב שגוי.
  - תיקון: לעטוף ב-log ברור "FLW: lookback=N אבל היו רק M זמינים — לוקח רוב על M".

- [ ] **#8 — `min_btc_drift_pct` filter יכול להחזיר רשימה ריקה גם כשיש history**
  `engine/history_tracker.py:130-144` (get_last_window_winners) + `engine/strategy_runner.py:788`
  - מה קורה: אם המשתמש מגדיר drift_pct גבוה (למשל 0.5%), ואין חלון בlookback האחרון עם תזוזה כזו → רשימה ריקה → fallback ל-side_preference (שבקונפיג הנוכחי הוא `signal`, שעלול לחזור על הצד שדווקא הפסיד).
  - השפעה: FLW יבוטל "בשקט". מהמשתמש זה ייראה כאילו FLW לא עובד.
  - תיקון: לוג ברור על "FLW: כל החלונות סוננו ע"י min_drift_pct=X — לאחרון תזוזה Y%". אופציה משלימה: להגיש fallback גמיש (לקחת את הראשון בלי סינון).

- [ ] **#9 — recorder לא רושם side_won=None כשמחיר חסר**
  `engine/main.py:492-503`
  - מה קורה: אם btc_open או btc_close הם None, `side_won` נשאר None ונרשם. ה-INSERT OR IGNORE ב-record_window_result מקבל את זה.
  - השפעה: `get_last_window_winners` מסנן `side_won IS NOT NULL` בשאילתה, אז הוא לא רואה את החלון הזה. אבל אז יש פער היסטורי, ו-FLW מקבל "פחות" נתונים מהמתוכנן.
  - תיקון: ב-backfill #5 אפשר למלא את החלונות החסרים. גם: לרשום `side_won` = "unknown" במקום None ולסנן את זה בנפרד, כך שהמערכת יודעת שניסתה ונכשלה.

### Low

- [ ] **#10 — `fetch_btc_spot_usdt` משמש לקבועי תיעוד נוספים שיכולים להיות חשודים**
  `engine/btc_price.py:203-221`
  - cache של 1s + fallback של 30s. השאלה האם אי-פעם משתמש דורש בדיקה במחיר עתידי דחוף ומקבל ספוט בן 30 שניות. כרגע לא נראה משפיע, אבל סימן לבדיקה עתידית.

- [ ] **#11 — FLW לא לוקח בחשבון את ה-window_sec שבו אכן נסחר הבוט**
  - אם המשתמש יחליף בין 5m ל-15m, get_last_window_winners מסנן לפי window_sec הנוכחי, מה שגורם ל-history יחסי (5m או 15m) — נכון לוגית, אבל אם המשתמש פשוט הסתכל לאחור על history של 5m ב-Dashboard בזמן שעבר ל-15m, יראה אי-עקביות עם FLW.

- [ ] **#12 — אין הגנה מפני "BTC ספוט אקסטרים" בזמן רישום**
  - אם Binance מחזיר ספוט חריג (glitch, שגיאת רשת מאוחרת) → ה-recorder ירשום תוצאה שגויה. תיקון #1 חוסם את זה כי נעבור ל-kline.

---

## עדיפויות לתיקון

1. **קודם כל #1 + #2** — שני התיקונים הקריטיים. שורות קוד ספורות, חוסם את הבאג הגדול. אחרי שיתוקנו, FLW יבחר את הצד הנכון בעקביות עם הבוט עצמו.
2. **#3 ביחד עם #1** — נקודה אחת בקוד שתפתור את הרבה (כתיבת history.db מתוך strategy_runner ברגע ה-rollover).
3. **#4** — defense against Binance kline lag.
4. **#5** — backfill בעלייה.
5. השאר — שיפורים איכותיים.

---

## רגרסיה צפויה אחרי תיקון

- FLW יבחר עקביות עם הבוט (אותו צד שהבוט "ראה" כניצחון).
- אחרי הפסד, Loss Recovery יכפיל **בכיוון הנכון** (כפי שהמשתמש רוצה).
- ה-Dashboard "חלון קודם" יחזיק נתון יחיד וקבוע (לא יזוז 10 שניות אחרי סיום חלון).
- אין סיכון לפוזיציות פתוחות — שינוי במקור הנתונים, לא בלוגיקת ביצוע ההזמנות.
