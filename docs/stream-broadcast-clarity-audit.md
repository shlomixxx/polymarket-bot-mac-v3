# Broadcast Stream Audit — `?stream=7&fit=1#stream-stats`

מסמך זה מסכם את הניתוח ואת ההצעות לשיפור דף השידור החי של הבוט, כפי שנראה לצופים (OBS / טלגרם / דפדפן מלא).

טווח הבדיקה מוגבל ל-Layout הייעודי של שידור בלבד:
- קובץ עיקרי: [src/StreamLiveBroadcastLayout.tsx](src/StreamLiveBroadcastLayout.tsx)
- רכיב-אב שמזין את ה-props: [src/LiveStreamTrade.tsx](src/LiveStreamTrade.tsx)
- (התייחסות בלבד, לא לשינוי) בוט טריגר/DCA: [src/TriggerTrader.tsx](src/TriggerTrader.tsx)

---

## 1. מצב נוכחי — מה באמת רואים בדף

### 1.1 מעל ה-fold
- **"LIVE NOW — Join before next trade closes"** — באנר אדום פועם בראש הדף.
- **כותרת "LIVE TRADE – BITCOIN"** — גופן 28px/36px (תלוי מצב `fb`).
- **שורת תאריך/חלון שעות** — גופן 13px, אטומיות 55%.
- **"Bot run · Xh Ym Zs"** — שורה קטנטנה ומוחלשת, גופן 11px ואטומיות **28% בלבד**. זה ה"שעון של הבוט" שהמשתמש ביקש להבליט ([StreamLiveBroadcastLayout.tsx:518-530](src/StreamLiveBroadcastLayout.tsx#L518-L530)).

### 1.2 שלוש קוביות סטטיסטיקה (`lb-stat-box`)
- **PNL** — ירוק/אדום, 28px.
- **WIN RATE** — צהוב, 28px.
- **TIME LEFT** — צהוב, 28px (זה ה-countdown של החלון הנוכחי, לא זמן ריצת הבוט).
  - ללא גרדיאנט/glow נפרד לכל קובייה — רואים שלוש תיבות דומות.

### 1.3 כרטיסי UP / DOWN
- שני כרטיסים גדולים (36/48px), ברורים מאוד.

### 1.4 `#stream-stats` — גרף + LAST TRADES
- **גרף PnL מצטבר** בצד שמאל (flex 3).
- **Aside "LAST TRADES"** בצד ימין (flex 1) — מציג עד 6 סבבים סגורים ([StreamLiveBroadcastLayout.tsx:1412-1559](src/StreamLiveBroadcastLayout.tsx#L1412-L1559)).
- כפתור **Hide/Show P&L** בתוך ה-aside.

### 1.5 מה **לא** מוצג כרגע לצופה
- **האם הבוט פעיל בכלל** (`stratCfg.mode`: `off` / `semi` / `auto`) — לא נראה במפורש.
- **האם מצב טריגר / DCA-Pulse פועל** — אין אינדיקציה כלל.
- **מה הבוט עושה עכשיו** (status / status_log של TriggerEngine) — לא מוצג.
- **המתנות פעילות** (למשל DCA slice 2/4 — Up, או "waiting X seconds").
- **זמן ריצת הבוט** — קיים אבל קבור בפונט 11px ואטומיות 28%.

---

## 2. ההצעות לשיפור

### 2.1 השעון של הבוט — להבליט (בקשה מפורשת של המשתמש)

**מצב נוכחי** ([StreamLiveBroadcastLayout.tsx:518-530](src/StreamLiveBroadcastLayout.tsx#L518-L530)):
```tsx
<div style={{
  fontSize: 11,
  color: "rgba(255,255,255,0.28)",
  marginTop: 6,
  fontWeight: 500,
  letterSpacing: "0.03em",
  fontVariantNumeric: "tabular-nums",
}} title="Wall time since semi/auto was enabled">
  Bot run · {botRunDisplay}
</div>
```

**הצעה** — להפוך ל-"chip" פועם עם אייקון ♦, גופן גדול יותר, וצבע בהיר:
- גופן: `fontSize: 15` (או 18 כשה-`fb` כבוי).
- אטומיות: `rgba(251, 191, 36, 0.95)` (צהוב תואם למותג הדף).
- Pill עם רקע זכוכיתי: `padding: "6px 14px"`, `borderRadius: 999`, `border: "1px solid rgba(251,191,36,0.45)"`.
- אייקון הדופק `●` מצומצם עם אנימציית `lbLivePulse` (כבר קיים ב-CSS).
- תווית ברורה לצופה דובר אנגלית: **"BOT RUNNING · 1h 24m 03s"**.
- כאשר `stratCfg.mode === "off"` — להציג במקום זה `"BOT IDLE"` באפור.

**הערה חשובה**: יש להימנע מהזזת הכותרת "LIVE TRADE – BITCOIN" ומ-CLS (layout shift). ה-pill יישב בדיוק באותו מיקום ובאותו גובה קבוע (למשל `minHeight: 28`).

### 2.2 הצגת פעילות הבוט — "Bot Activity"

**מקור הנתונים**: `GET /api/trigger/state` (כבר קיים, מוגש ב-[engine/main.py](engine/main.py) בשורה 1532).
שדות רלוונטיים ב-`to_dict()`:
- `active: boolean`
- `mode: "off" | "momentum" | "signal" | "dca_pulse"`
- `status: string` — מה הבוט עושה כרגע (טקסט חופשי).
- `status_log: {ts:number; msg:string}[]` — היסטוריית הודעות.
- `dca_running: boolean`, `config.dca_pulse_slices`, `config.dca_pulse_direction`.
- `cooldown_remaining`, `current_window_epoch`.

**הנחיית עבר (feedback מהמשתמש)**: **לא להוסיף חלון נפרד** — לשלב בתוך ה-aside של LAST TRADES.

**הצעת מימוש** — תוספת קומפקטית בראש ה-aside LAST TRADES, בין הכותרת לרשימת הסבבים:

```
LAST TRADES                         [Hide P&L]
─────────────────────────────────────────────
◆ DCA-PULSE · UP · slice 2/4
  16:42:05  Bought UP @ 47¢
  16:41:38  Waiting cooldown 00:12
  16:40:52  Trigger fired — momentum Up
─────────────────────────────────────────────
● 16:35 UP  +$24
● 16:30 DOWN -$12
...
```

מאפיינים:
- שורה ראשונה (**בולטת**): מצב + כיוון + slice אם רלוונטי, רקע זכוכיתי קטן.
- 3–5 שורות status_log אחרונות עם שעה `HH:MM:SS`.
- צבע שורה לפי מילות מפתח (`bought/bought down` אדום, `sold/sold up` ירוק, `waiting/cooldown` אפור, `trigger` צהוב).
- כשאין טריגר פעיל (`mode === "off"` או `active === false`): הצג טקסט אחד דיסקרטי — **"No bot trigger active — trading via live signal only"**.
- ה-block מקופל למקסימום ~120px גובה כדי שלא ידחוף את LAST TRADES.

**אופן ה-polling** (ב-[LiveStreamTrade.tsx](src/LiveStreamTrade.tsx)):
- `setInterval(fetch /api/trigger/state, 2000)` — תואם לקונבנציה הקיימת.
- מותנה ב-`isPageHidden()` כדי לחסוך בקריאות כשהטאב לא גלוי.
- עטוף ב-`safeApi<T>` שכבר בשימוש במקומות אחרים.
- ה-state מועבר כ-prop חדש `triggerState?: StreamTriggerState | null` ל-`StreamLiveBroadcastLayout`.

### 2.3 ניתוח UX/בהירות — שיפורים נוספים מומלצים

דירוג: **H** = השפעה גבוהה על צופה, **M** = בינונית, **L** = נמוכה/טעם.

#### H-1. "TIME LEFT" לא מסביר על מה הזמן
- כיום כתוב רק "TIME LEFT" והרוב יחשבו שזה זמן ריצת הבוט.
- **הצעה**: לשנות ל-"WINDOW ENDS IN" או להוסיף tagline קטן מתחת `until next round close`.

#### H-2. PNL ללא תג "Session" / "Today"
- הצופה לא יודע האם המספר הוא לכל הזמנים / היום / הסשן.
- **הצעה**: תווית משנה קטנה מתחת ל-PNL: `Session (2h 14m)` — מסתנכרן אוטומטית עם ה-uptime.

#### H-3. WIN RATE בלי מכנה
- "60%" לא אומר כלום בלי לדעת כמה עסקאות.
- **הצעה**: להוסיף שורת שנייה בגודל 10-11px: `12 W / 8 L (20 total)`.

#### H-4. UP / DOWN — הצופה לא יודע מה המשמעות
- המחיר ב-¢ ברור למשתמשי Polymarket, פחות לצופים חיצוניים.
- **הצעה**: תווית משנה קטנה מתחת: `Price of "Yes" share` + בסוגריים `implied prob`.

#### H-5. אין אינדיקציית "האם אני רואה דמו או כסף אמיתי"
- ה-props `isLive` / `liveAccountUsd` / `demoBalanceUsd` קיימים אבל לא בולטים.
- **הצעה**: פס עליון קטן: `REAL MONEY · Polymarket` (ירוק) או `DEMO MODE` (אפור) — צמוד לבאנר LIVE NOW.

#### M-6. LAST TRADES — אין סיכום מהיר
- הצעה: שורה קומפקטית מעל הרשימה: `Last 6 rounds: 4W 2L · +$58`.

#### M-7. גרף PNL חסר **אפס** בולט
- הצופה לא מבחין מתי חצינו רווח/הפסד.
- **הצעה**: קו אפס אנכי דק יותר + תג "$0" צמוד.

#### M-8. באנר טלגרם ממוקם מעל המובייל-fold
- בדפים קצרים (OBS 1080p) הטלגרם לוקח מקום מרכזי. זה תקין לשיווק, אבל שווה לבדוק שהוא לא חופף ל-STAT BOXES ב-`fb=1`.

#### L-9. גופנים ב-`fb=1` — מדרג אחיד
- היום יש ערבוב: 11/13/14/28/36/48. כדאי להוריד את ה-13 ל-12 ואת ה-11 ל-10 (עקביות).

#### L-10. "Free access limited time" — טקסט שיווקי גנרי
- הצעה: להחליף ב-CTA ספציפי: `Signup closes at round end` / `Next signal in 00:42`.

---

## 3. סדר עדיפויות למימוש

**שלב 1 (בקשה מפורשת):**
1. הבלטת שעון הבוט (2.1).
2. שילוב Bot Activity בתוך LAST TRADES (2.2).

**שלב 2 (שיפורי בהירות גבוהים):**
3. H-1 — תיקון תווית TIME LEFT.
4. H-3 — מכנה ל-WIN RATE.
5. H-5 — תג REAL/DEMO.

**שלב 3 (polish):**
6. H-2, H-4, M-6, M-7.

**שלב 4 (טעם):**
7. M-8, L-9, L-10.

---

## 4. סיכום קבצים שיושפעו (אם תאושר הרצה)

- [src/StreamLiveBroadcastLayout.tsx](src/StreamLiveBroadcastLayout.tsx) — JSX ו-styles: סעיפים 2.1, 2.2, וכל השיפורים בסעיף 2.3.
- [src/LiveStreamTrade.tsx](src/LiveStreamTrade.tsx) — polling של `/api/trigger/state`, state חדש, העברת prop.
- ללא שינויי Backend — כל הנתונים כבר חשופים ב-`/api/trigger/state` וב-props הקיימים.

---

## 5. נקודות להחלטה לפני מימוש

1. **גודל/סגנון שעון הבוט**: pill צהוב תואם מותג, או pill לבן נייטרלי? (המסמך הציע צהוב).
2. **גובה מקסימום ל-Bot Activity** בתוך LAST TRADES: 120px או 160px? (משפיע על כמות השורות שיראו).
3. **שפת הטקסט לצופים**: אנגלית בלבד (כמו היום) או דו-לשוני?
4. **אישור כללי**: לממש את כל שלב 1 בלבד, או גם שלב 2?

ממתין להנחיה לפני נגיעה בקוד.
