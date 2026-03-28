# תוכנית מפורטת: שיפור יומן וניתוח עסקאות

## רקע

המשתמש רצה:
1. **הבנה ברורה** — מה אירוע בפועל (כניסה, יציאה) לעומת סטטוס (מחירים, תנאים).
2. **יומן לפי מסכל** — לראות את כל ההיסטוריה של אותה עסקה תחת לשונית אחת.
3. **מסלול רווח/הפסד** — לראות שיא/שפל לאורך העסקה ולעזור באופטימיזציה.

---

## מה יושם (מצב נוכחי)

### 1. הפרדה בין אירועים לסטטוס

**Backend — strategy_runner.py**

- `log_event(msg, session_id)` — לאירועים בפועל (כניסה, יציאה, DCA).
  - מתווסף הקידומת `▶ אירוע:` כדי להבחין מסטטוס.
  - כל אירוע נשמר עם `session_id` לקישור למסכל.
- `status(msg, session_id?)` — נשאר לסטטוסים, עם `session_id` אופציונלי כשמנהלים פוזיציה קיימת.
- הודעת "נכנס לפי limit" שונתה ל־"ממתין ל-limit fill" כדי שלא תיראה כאילו נכנסנו בפועל.
- אירועי כניסה/יציאה משתמשים ב־`log_event` במקום `log`.
- כניסה אוטומטית מציינת גם DCA: `נכנס לעסקה (אוטומטי) — DCA 2/8: Up ×124 @ 0.46`.

### 2. שמירת לוג לפי session

**StrategyRuntime**

- `log_entries: list[dict]` — רשומות מובנות:
  - `ts` — זמן.
  - `msg` — טקסט ההודעה.
  - `type` — `"event"` | `"status"` | `"system"`.
  - `session_id` — מזהה המסכל (אם רלוונטי).

**API**

- `GET /api/strategy/log-entries` — מחזיר עד 300 רשומות אחרונות עם `session_id`.

**סטטוסים עם session_id**

- TP נעול עד DCA, דקה אחרונה, קרוב ל-TP, אזור ביניים עם פוזיציה, ממתין ל-limit fill.
- מאפשר לקשר סטטוסים לעסקה ספציפית.

### 3. הצגת pnl_path ויומן במסכל

**Frontend — TradesBySession**

בפתיחת מסכל מוצגים:

1. **גרף מסלול רווח/הפסד** — `pnl_path` מהיציאה או מ־`last_mark.legs` (פוזיציות פתוחות).
   - ציר X: זמן.
   - ציר Y: רווח לא ממומש באחוזים.
   - קו אפס מסומן.
2. **יומן העסקה** — רשומות היומן עם `session_id` תואם.
   - עד 20 הרשומות האחרונות, עם ציון אירוע vs סטטוס.
3. **טבלת עסקאות** — כמו קודם.

### 4. שיא/שפל בזמן אמת

**Backend — demo_engine.py**

- בכל `mark_to_market` מתווספים ל־`last_mark.legs`:
  - `peak_unrealized_pct` — שיא רווח לא ממומש
  - `trough_unrealized_pct` — שפל רווח לא ממומש
  - `pnl_path` — מסלול הדגימות (גם לפוזיציות פתוחות)

**Frontend**

- לפוזיציות פתוחות: שיא/שפל נלקחים מ־`last_mark.legs` (מתעדכן כל שנייה).
- לפוזיציות שנסגרו: נעשה שימוש ב־`peak`/`trough` מהיציאה.

### 5. TP בדקה אחרונה ו־DCA override

**strategy_runner.py**

- **דקה אחרונה (freeze):** מאפשרים TP גם כש־DCA לא הושלם — אי־אפשר להוסיף DCA בכל מקרה.
- **dca_tp_override_pct** (ברירת מחדל 50%): כשהרווח הלא ממומש ≥ X%, מאפשרים TP גם בלי השלמת כל הסלייסים. מונע לאבד רווח ענק (למשל 134% שיא שלא נוצל → EXPIRE בהפסד).

### 6. איפוס

- במעבר מ־off למצב פעיל: `log_entries` מתאפס יחד עם `log_lines`.

### 7. לוגים תמיד ומסודרים לפי מחזור עסקה

**run-bot.command**

- תמיד מריץ `run-with-logs.sh` — לוגים מלאים בכל הרצה.

**main.py**

- `_ensure_log_run_dir()`: אם `LOG_RUN_DIR` לא הוגדר, יוצר אוטומטית `logs/runs/YYYY-MM-DD/HH-MM-SS` ו־meta.json.

**run_logging.py**

- `write_journal_by_session(log_entries)`: מקבץ רשומות לפי `session_id`.
- `journal_by_session.json` — מבנה: `{sessions: {sid: [entries]}, no_session: [...]}`.
- `journal_by_session.txt` — קריא: בלוק לכל מחזור עסקה עם רשימות האירועים.
- `strategy_snapshot.json` — כולל `log_entries` (עד 500).
- מתעדכן כל ~60 שניות ב־periodic_snapshot_loop.

### 8. נתונים לזיהוי בעיות, תקיעות ורווחים/הפסדים

**run_logging.py**

- `demo_summary` מורחב: `equity_usd`, `unrealized_usd`, `total_realized_pnl`, `recent_trades_pnl`, `positions_summary` (עם upnl לפוזיציות פתוחות).
- `diagnostics`: `seconds_since_last_tick`, `potentially_stuck` (אם >90 שניות במצב אוטו).
- `run_diagnostics.txt` — קובץ טקסט קריא: סטטוס מערכת, רווחים/הפסדים, פוזיציות פתוחות, עסקאות אחרונות.
- `log_error(msg, context)` — רישום שגיאות ל־events.jsonl עם `event: "error"`.

**strategy_runner.py**

- קורא `log_error` כשכניסה/גידור נכשלים או כשנזרקת חריגה ב־_tick.

---

## מבנה קבצים

```
engine/
  strategy_runner.py   — log_event, status(session_id), log_entries, dca_tp_override_pct, TP-freeze override
  main.py              — GET /api/strategy/log-entries, dca_tp_override_pct ב-ConfigBody
  demo_engine.py       — peak/trough/pnl_path ב-last_mark.legs (זמן אמת)
src/
  App.tsx              — TradesBySession עם pnl_path, יומן, lastMark, liveLeg; dcaTpOverridePct
```

---

## זרימת נתונים

```
מנוע: log_event("נכנס...", session_id=tr["session_id"])
  → log_lines (מחרוזות)
  → log_entries (אובייקטים עם session_id)

מנוע: status("סטטוס: ...", session_id=sid)
  → log_lines
  → log_entries

Frontend: refresh
  → GET /api/strategy/log-entries
  → setLogEntries(entries)

Frontend: TradesBySession
  → sessionLogs = logEntries.filter(e => e.session_id === sid)
  → pnlPath = lastExit.pnl_path ?? liveLeg.pnl_path
  → peak/trough = lastExit ?? liveLeg (מתעדכן בזמן אמת)
  → גרף + יומן + טבלה
```

---

## שיפורים עתידיים אפשריים

1. **דחיית כניסה** — כש-DCA ממתין (למשל מרווח זמן), להוסיף אירוע:
   `"דחיית כניסה — ממתין Xs לסל הבא (1/8)"` במקום רק סטטוס.
2. **ייצוא יומן לפי מסכל** — ב־export_csv או קובץ נפרד, לכל session רשומות היומן.
3. **סינון ביומן** — במסך אסטרטגיה: רק אירועים, או רק סטטוס, או לפי מסכל.
4. **מיקוד על "למה לא נכנסנו"** — כאשר התנאים היו ל־limit אבל לא התבצע fill, לוג ייעודי עם הפרט.

---

## בדיקות

- הרצת בוט עם DCA, בדיקת כניסות ויציאות.
- פתיחת מסכל בטאב סטטיסטיקה — גרף pnl_path ויומן העסקה.
- בדיקת `GET /api/strategy/log-entries` והחזרת `session_id` ברשומות.
- פוזיציה פתוחה: שיא/שפל מתעדכנים כל שנייה (last_mark.legs).
- DCA חלקי + רווח גבוה: TP מתבצע כשעוברים dca_tp_override_pct (ברירת מחדל 50%).
- דקה אחרונה + DCA חלקי: TP מותר (freeze override).
