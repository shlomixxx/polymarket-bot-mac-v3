# פרומפט מוכן ל-Claude — לייב Polymarket: כניסות, יציאות ובדיקות

הדבק את הבלוק הבא בשיחה חדשה עם Claude (או מודל דומה). התאם בסוגריים אם צריך.

---

## הפרומפט (להעתקה)

```
אתה עוזר טכני לפרויקט בוט Polymarket (מסחר CLOB אמיתי). המטרה: שהמשתמש יוכל לקבל עסקאות BUY ולמכור SELL בחשבון לייב בלי תקלות חוזרות, ולבצע בסוף טייסט (בדיקות) שמוכיח שהכל עובד.

### הקשר פרויקט (נתיב מקומי)
- ריפו: polymarket-bot-mac-v3
- מנוע Python: `engine/` — בעיקר `strategy_runner.py`, `live_clob.py`, `demo_engine.py`, `main.py`
- ממשק: `src/App.tsx`
- משתני סביבה: `.env.example`, `.env` (לא לחשוף סודות בצ'אט)
- רשימת בדיקות ידנית/הגדרות: `docs/system-readiness-checklist-he.md`

### מה לקרוא קודם (Skills / מסמכים פנימיים)
1. קרא את `docs/system-readiness-checklist-he.md` — זה ממסגר את שאלות החשבון, USDC.e, allowance, חתימה (POLYMARKET_SIGNATURE_TYPE / FUNDER), reconcile.
2. עיין ב-`.env.example` — הסבר על מפתח, funder, POLYMARKET_LIVE.
3. אם יש `.cursor/rules/railway-stream-performance.mdc` — רק אם הפריסה על Railway/סטרים.

### בעיות שכבר זוהו בקוד/התנהגות (לוודא שלא חוזרות)
- Drift בין יומן פנימי (shadow) לבין CLOB → reconcile, SETTLE ב-rollover, שגיאות SELL `not enough balance`.
- DCA עם `max_entries_per_window=1` שחסם סלייס שני — תוקן בלוגיקה (המשך DCA לא נחסם על ידי מקס׳ כניסות).
- TP שדילג כשפוזיציה מתחת למינימום חוזים של השוק — עכשיו מנסים סגירה בכל זאת.
- כניסה לייב נכשלת כשאין מספיק USDC מול תקציב סלייס DCA × מכפיל שחזור הפסד — ביומן יש פירוט (בסיס, מכפיל, תקציב לסלייס).

### משימותיך (בסדר)
1. **תצורה**: וודא שהמשתמש מבין וממלא נכון חתימה, funder (אם proxy), POLYMARKET_LIVE, יתרה ו-allowance — לפי הצ'קליסט. אל תבקש להדביק מפתח פרטי בצ'אט.
2. **הגדרות מסחר**: הסבר את הקשר בין investment_usd, שחזור הפסד (מכפיל), DCA (מספר סלייסים), max_entries_per_window, מינימום חוזים — כדי שלא ייחסם TP או סלייס.
3. **קוד**: אם משהו עדיין שבור — הצע תיקון ממוקד בקבצים הרלוונטיים; אל תרחיב רפקטור בלי צורך.
4. **טייסט חובה (בסוף)** — הרץ ובדוק:
   - `python3 -m pytest engine/tests/ -q` (או לפחות `test_demo_engine.py`, `test_strategy_runtime.py`, `test_losing_trades_stats.py`, `test_live_reconcile.py` אם קיימים).
   - אם יש `npm test` / `npm run build` לפרונט — הרץ והצג תוצאה.
   - רשימת בדיקה ידנית קצרה: (א) יתרה ב-Polymarket תואמת ללוח אחרי reconcile (ב) כניסה לייב קטנה אם המשתמש מאשר (ג) יציאה/TP או SELL לא מחזירה 400 על balance אם היומן מסונכרן.

### פלט מצופה
- סיכום מה בוצע / מה הומלץ.
- פלטי הפקודות מהטייסט (pytest וכו').
- רשימת "עשה / אל תעשה" קצרה למשתמש לפני מסחר לייב אמיתי.

### אילוצים
- לא לשמור סודות בקבצים שמועלים ל-git.
- להשתמש בתיעוד הרשמי של Polymarket CLOB כשיש ספק (quickstart, create order, deposit).
```

---

## הערות לך (בעל הפרויקט)

1. **החלף** `נתיב מקומי` אם העתקת הפרויקט במקום אחר.
2. אם אתה משתמש ב-**Cursor Skills** — אפשר להוסיף בשורה "Skills" הפניה לקבצי SKILL שלך (למשל `create-rule`, או כלל פרויקט ב-`.cursor/rules/`).
3. **טייסט אמיתי עם כסף** — Claude לא יכול להחליף אותך; אחרי pytest תבדוק בעצמך בחשבון עם סכום קטן או במצב שאתה מוכן להסכן.
4. שמור את הפרומפט הזה ליד `docs/system-readiness-checklist-he.md` — הם משלימים אחד את השני.

---

*קובץ זה נוצר לשימוש כפרומפט חיצוני ל-Claude; עדכן לפי גרסת הפרויקט.*
