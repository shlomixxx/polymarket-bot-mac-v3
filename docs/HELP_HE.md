# עזרה ותיעוד — מדריך מפורט

מסמך זה מסביר כל פקד, מצב, והגדרה בתוכנה. מיועד להצגה בלשונית **"עזרה ותיעוד"**.

---

## 1. מבוא — מה התוכנה עושה?

התוכנה סוחרת בשווקי **Polymarket BTC Up/Down** (חלונות של 5 או 15 דקות) — שווקים בינאריים שנסגרים ב-$1.00 למנצח וב-$0.00 למפסיד. התוכנה תומכת ב:
- **סימולציה (דמו)** — הזמנות וירטואליות מול ספר ההזמנות החי.
- **מסחר חי (CLOB)** — הזמנות אמיתיות מול Polymarket (דורש `py-clob-client` ומפתח תקף).

---

## 2. לשוניות (Tabs)

| לשונית | מה היא מציגה |
|---|---|
| **לוח בקרה (dash)** | מצב השוק הנוכחי, יתרת חשבון, פוזיציות פתוחות, מחירי bid/ask חיים. |
| **אסטרטגיה (strategy)** | כל הפרמטרים הקריטיים: סכום השקעה, TP, DCA, Loss Recovery, Hold-to-Resolution וכו'. |
| **📡 סיגנלים** | סיגנלים חיים מקנוניקל מחירי BTC ומסקנות הכניסה של המנוע. |
| **⚡ מסחר מהיר (trigger)** | כניסה ידנית מיידית לשוק לפי תנאי trigger. |
| **סטטיסטיקה (דמו)** | היסטוריית עסקאות בסימולציה, רווח מצטבר, עסקאות פתוחות. |
| **סטטיסטיקה לייב** | היסטוריית עסקאות אמיתיות ב-CLOB. |
| **ניתוח v3 / 📊 אנליטיקס V3** | ניתוח עומק של ביצועים, התפלגות רווחים/הפסדים, זמני מחזור. |
| **עזרה ותיעוד** | המסמך הזה. |

---

## 3. מצבי הפעלה (mode)

- `off` — המנוע עצור; אין כניסות אוטומטיות.
- `semi` (חצי-אוטומטי) — המנוע מסמן סיגנלים אבל לא שולח הזמנות. הכנסה דורשת אישור ידני.
- `auto` — אוטומטי מלא; כניסות, DCA, ויציאות — הכל אוטומטי.

**דמו לעומת לייב:** המתג `live_trading` (בלוח הבקרה) קובע אם הזמנות נשלחות ל-CLOB אמיתי. כברירת מחדל `false` — סימולציה בלבד.

---

## 4. פרמטרי אסטרטגיה — מילון מפורט

### 4.1 גודל השקעה (Investment Sizing)

| פרמטר | הסבר |
|---|---|
| `investment_mode` | `fixed` = סכום דולר קבוע לכניסה. `percent` = אחוז מהון התיק הנוכחי (equity), מתעדכן אוטומטית כשהתיק גדל/קטן. |
| `investment_usd` | הסכום לכניסה במצב `fixed` (למשל $25). |
| `investment_pct_of_portfolio` | האחוז מהון התיק במצב `percent` (למשל 5% → $50 כשההון $1000). |
| `min_contracts` | מספר חוזים מינימלי לכניסה. Polymarket דורש לרוב ≥5. |
| `entry_price_cents` | מחיר היעד בסנטים (ברירת מחדל 50) — המחיר ה"אידיאלי" לכניסה. |

**חשוב:** כש-`loss_recovery_enabled=true`, המכפיל מוכפל על בסיס הסכום שחושב (fixed או percent).

### 4.2 שוק ועיתוי

| פרמטר | הסבר |
|---|---|
| `btc_window` | `5m` או `15m` — אורך חלון השוק. |
| `min_minutes_for_entry` | מינימום דקות שנותרו בחלון כדי לאשר כניסה (למשל 3.0). |
| `freeze_last_minutes` | כמה דקות לפני סוף החלון לחסום כניסות חדשות (ברירת מחדל 1.0). |
| `intermediate_block_new_entries` | אם `true`, בזמן "אזור ביניים" (פחות מ-`min_minutes_for_entry` אבל יותר מ-`freeze_last_minutes`) — חסום כניסות חדשות, אבל אפשר DCA על פוזיציות קיימות. |

### 4.3 Take Profit (יעד רווח)

| פרמטר | הסבר |
|---|---|
| `take_profit_pct` | אחוז רווח מעלות הכניסה הממוצעת → יציאה. למשל 50 = יציאה ב-+50% מהעלות. |
| `dca_tp_override_pct` | TP שונה אחרי DCA (ברירת מחדל זהה ל-`take_profit_pct`). |
| `near_tp_pct` | "אזהרת קרבה" ל-TP — רק ל-UI. |

### 4.4 DCA (Dollar Cost Averaging)

| פרמטר | הסבר |
|---|---|
| `dca_enabled` | הפעלת DCA — תוספת חוזים אם המחיר ירד. |
| `dca_slices` | מספר פרוסות DCA מקסימלי (ברירת מחדל 3). |
| `dca_interval_sec` | מרווח מינימלי בשניות בין פרוסות. |
| `dca_discount_enabled` | אם `true`, DCA מחכה לירידת מחיר. |
| `dca_discount_pct` | אחוז ירידה מהעלות הממוצעת שנדרש ל-DCA (למשל 20%). |

**דוגמה:** כניסה ב-0.20, `dca_discount_pct=20` → DCA הבא יחכה ל-bid ≤ 0.16.

### 4.5 Loss Recovery (שחזור הפסד)

| פרמטר | הסבר |
|---|---|
| `loss_recovery_enabled` | הפעלה. |
| `loss_recovery_step_pct` | באיזה אחוז להגדיל את סכום הכניסה אחרי כל N הפסדים (למשל 200 → להכפיל פי 3 = בסיס + 200%). |
| `loss_recovery_every_n_losses` | אחרי כמה הפסדים להגדיל (1 = אחרי כל הפסד). |
| `loss_recovery_max_multiplier` | תקרת מכפיל (למשל 200 = עד פי 200 מהבסיס). |

**איפוס:** בכל TP מוצלח המכפיל חוזר ל-1.00×.

**⚠️ אזהרה:** הכפלת סכום אחרי הפסד מגדילה חשיפה אקספוננציאלית. אם יש רצף הפסדים ארוך — החשבון יכול להתנקות.

### 4.6 Hold-to-Resolution (החזקה עד סגירת השוק)

מצב שבו העסקה **לא יוצאת ב-TP רגיל**, אלא מחזיקה עד סגירת החלון ($1 אם ניצחה, $0 אם הפסידה).

| פרמטר | הסבר |
|---|---|
| `hold_to_resolution_enabled` | הפעלת המצב. |
| `hold_to_resolution_min_dca_slices` | מינ׳ DCA slices שבוצעו כדי להפעיל החזקה. 0 = להחזיק כבר מכניסה ראשונה. |
| `hold_to_resolution_min_price` | מחיר מינימלי של הפוזיציה כדי להפעיל החזקה (למשל 0.85 = רק אם המחיר כבר עבר 85¢). |
| `hold_to_resolution_stop_loss_enabled` | אם `true`, יציאה מוקדמת במקרה של נפילה חדה (stop-loss גם במצב החזקה). |

**⚠️ סיכון חמור:** אם הסתברות הניצחון נמוכה, החוזה נסגר ב-$0 ואיבדת 100% מהפוזיציה. שקול לשלב עם `stop_loss_enabled`.

### 4.7 Peak Watchdog (שומר השיא)

מנגנון שעוקב אחרי שיא הרווח ויוצא אם המחיר מתרחק ממנו.

| פרמטר | הסבר |
|---|---|
| `peak_watchdog_enabled` | הפעלה. |
| `peak_retreat_exit_pct` | אחוז נסיגה מהשיא → יציאה (למשל 2 = יציאה אם ירד 2% מהשיא). |

### 4.8 Hedge (גידור)

| פרמטר | הסבר |
|---|---|
| `hedge_enabled` | פתיחת פוזיציות בשני הכיוונים במקביל. |
| `hedge_combined_ask_max` | סכום ה-ask המשולב המקסימלי (Up+Down) — למשל 0.98 (סכום פחות מ-$1 = רווח מובטח בתיאוריה). |
| `side_preference` | `signal` = לפי הסיגנל. `up`/`down` = הימור קבוע. |

### 4.9 Auto Re-entry (כניסה חוזרת)

| פרמטר | הסבר |
|---|---|
| `auto_reenter_after_tp` | כניסה מיידית אחרי TP מוצלח. |
| `reenter_cooldown_sec` | השהיה בשניות לפני כניסה חוזרת. |
| `max_entries_per_window` | תקרת כניסות בחלון יחיד. |
| `max_trades_per_hour` | תקרת עסקאות לשעה. |
| `max_notional_per_window_usd` | תקרת היקף דולרי בחלון. |

### 4.10 ביצוע הזמנה (Order Execution)

| פרמטר | הסבר |
|---|---|
| `order_mode` | `market` = שליחה במחיר השוק. `limit` = הזמנה מוגבלת במחיר. |
| `entry_slippage_pct` | סטייה מותרת בכניסה (%). |
| `exit_slippage_pct` | סטייה מותרת ביציאה. |
| `retry_max_attempts` | ניסיונות חוזרים במקרה של דחייה. |

---

## 5. תרחישים מומלצים

### תרחיש "שמרני למתחילים"
```
investment_mode: fixed, investment_usd: 10
take_profit_pct: 30
dca_enabled: false
loss_recovery_enabled: false
hold_to_resolution_enabled: false
peak_watchdog_enabled: true, peak_retreat_exit_pct: 2
```

### תרחיש "צמיחה אגרסיבית"
```
investment_mode: percent, investment_pct_of_portfolio: 5
take_profit_pct: 50
dca_enabled: true, dca_slices: 3, dca_discount_pct: 20
loss_recovery_enabled: true, loss_recovery_step_pct: 100, loss_recovery_max_multiplier: 8
hold_to_resolution_enabled: false
```

### תרחיש "החזקה לסגירה" (גבוה-סיכון, גבוה-תשואה)
```
hold_to_resolution_enabled: true
hold_to_resolution_min_dca_slices: 1
hold_to_resolution_min_price: 0.70
hold_to_resolution_stop_loss_enabled: true  ← חובה!
```

---

## 6. תקלות נפוצות

| סימן | סיבה אפשרית | פתרון |
|---|---|---|
| לא נשלחה פקודה | נותר פחות מ-`freeze_last_minutes` לחלון | חכה לחלון הבא או הקטן את הפרמטר. |
| "no balance" | אין יתרה בדמו/לייב | בדוק `balance_usd` בלוח הבקרה. |
| Hold-to-Resolution לא הפעיל | `min_dca_slices` או `min_price` לא התמלאו | הוריד את הספים או ודא שה-DCA עובד. |
| TP מוקדם מדי | `take_profit_pct` נמוך | העלה את הערך. |
| רצף הפסדים גדול עם LR | המכפיל רץ קדימה מדי | הוריד `loss_recovery_step_pct` או `max_multiplier`. |

---

## 7. הפעלה טכנית

```bash
cd engine && pip install -r requirements.txt
cd .. && npm install && npm run dev
```

- השרת החי: `http://localhost:5173`
- API המנוע: `http://localhost:8000/api/...`
- הגדרות נשמרות אוטומטית ל-[engine/config_persisted.json](engine/config_persisted.json) כ-1.5 שנ' אחרי שינוי ב-UI.

---

## 8. קבצים חשובים

| קובץ | תפקיד |
|---|---|
| [engine/main.py](engine/main.py) | FastAPI server + אנדפוינטים. |
| [engine/strategy_runner.py](engine/strategy_runner.py) | לוגיקת המסחר האוטומטי. |
| [engine/demo_engine.py](engine/demo_engine.py) | מנוע הסימולציה. |
| [engine/config_persisted.json](engine/config_persisted.json) | ההגדרות הנוכחיות (השומר עצמו כותב). |
| [engine/history.db](engine/history.db) | SQLite: כל העסקאות. |
| [src/App.tsx](src/App.tsx) | אפליקציית ה-UI הראשית (React). |

---

## 9. אזהרות חשובות

- **מסחר חי דורש הסכמה לתנאי השימוש של Polymarket ועמידה בדינים החלים באזור מגוריך.**
- **Hold-to-Resolution יכול להוביל להפסד מלא של הפוזיציה.**
- **Loss Recovery מגדיל חשיפה אקספוננציאלית — שים תקרת מכפיל שמרנית.**
- **תמיד בדוק הגדרות בסימולציה לפני הפעלה חיה.**
