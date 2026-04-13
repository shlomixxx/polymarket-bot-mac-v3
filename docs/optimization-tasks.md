# משימות — ביקורת ואופטימיזציית בקשות

עדכן את הקובץ הזה אחרי כל צעד משמעותי. סמן `[x]` רק כשבוצע בפועל.

## שלב 1 — ביקורת (מה לא בוצע / חלקי)

- [x] נקראו `.cursor/rules/railway-stream-performance.mdc` והותאמו ציפיות לפרויקט
- [x] סוכנו נקודות פולינג / קאש ב־`App.tsx`, `LiveStreamTrade.tsx`, וקבצים רלוונטיים (Signals, Trigger וכו')
- [x] נבדקו נתיבי API כבדים ב־`engine/main.py` (TTL, סערות אפשריות)
- [x] נבדק `src/api.ts` (timeout, התנהגות ברקע)

### ממצאי ביקורת

| סיווג | קובץ | שורות | תיאור |
|--------|------|-------|-------|
| **P1** | `src/SignalsPanel.tsx` | 444–448 | signals poll (5s) חסר `isPageHidden()` — ממשיך לרוץ ברקע |
| **P1** | `src/SignalsPanel.tsx` | 451–473 | contract-prices poll (2s) חסר `isPageHidden()` — ממשיך לרוץ ברקע |
| **P1** | `src/TriggerTrader.tsx` | 338–342 | trigger state poll (2s) חסר `isPageHidden()` — ממשיך לרוץ ברקע (endpoint in-memory, סיכון נמוך יותר) |
| **P2** | `src/SignalsPanel.tsx` | 444–448 | חסר in-flight guard — בקשות עלולות להצטבר אם השרת איטי |
| **P2** | `engine/main.py` | 1206–1251 | `/api/signals` חסר cache בצד שרת — כל קריאה מביאה 2× CLOB books |

**תקין — לא נדרש שינוי:**
- `App.tsx`: `refreshInFlight` mutex + `isPageHidden()` backoff (10s) ✅
- `LiveStreamTrade.tsx`: `refreshGeneration` counter + `isPageHidden()` ✅
- `App.tsx` / `LiveStreamTrade.tsx`: snapshot poll 500ms — in-memory בלבד, חסר CLOB ✅
- `engine/main.py`: `orderbook-summary` cache 2.0s + Lock, `contract-prices` cache 2.0s ✅
- `api.ts`: timeout 8s via AbortController ✅

**בדיקת רגרסיה (שלב 1):**
- `tsc --noEmit` ✅ PASS
- `npm test` (vitest) ✅ PASS — 1 file, 8 tests
- `npm run build:web` ✅ PASS — 850 modules

## שלב 2 — תכנון שיפורים (בלי הצפה, מהירות גבוהה)

- [x] הוגדרה אסטרטגיה: תדירות פולינג + cache שרת + אולי אצווה נתונים בנתיב קיים
- [x] הובטח שפעולות POST קריטיות לא נתקעות מאחורי סערת GET (או תועד למה לא רלוונטי)

### תכנון שבוצע

**P1 — שלוש נקודות קוד (בוצע):**
1. `src/SignalsPanel.tsx` — signals interval: הוספת `if (!isPageHidden())` ✅
2. `src/SignalsPanel.tsx` — contract-prices poll: הוספת `if (!active || isPageHidden()) return` ✅
3. `src/TriggerTrader.tsx` — trigger poll: הוספת `if (!isPageHidden())` ✅

**P2 — שיפורי שרת (אופציונלי, לא בוצע):**
4. `engine/main.py` — הוספת cache 2–3s ל-`/api/signals` (כמו `contract-prices`). רלוונטי רק עם מספר צופים; לא נדרש כעת.

**POST לא נחסם ע"י GET:** מאושר — כל POST הוא user-action ולא חלק מלולאת polling; שרת FastAPI async לא חוסם.

## שלב 3 — יישום

- [x] שינויים בקוד הוחלו (או תועד למה לא נדרש שינוי)
- [x] משתני env / Railway תועדו אם רלוונטי

### שינויים שבוצעו

| קובץ | שינוי |
|------|-------|
| `src/SignalsPanel.tsx` | הוספת `import { isPageHidden } from "./api"`; guard על signals poll (5s) ו-contract-prices poll (2s) |
| `src/TriggerTrader.tsx` | הוספת `import { isPageHidden } from "./api"`; guard על trigger state poll (2s) |

**משתני env:** אין שינוי נדרש. `ORDERBOOK_CACHE_TTL=2` (ברירת מחדל) מתאים לקצבי הפולינג.

### בדיקת רגרסיה (שלב 3)

- `tsc --noEmit` ✅ PASS
- `npm test` (vitest) ✅ PASS — 1 file, 8 tests
- `npm run build:web` ✅ PASS — 850 modules, 5.12s
- Linter: 0 errors

## חסמים / החלטות פתוחות

_(רשום כאן אם משהו דורש משתמש / סיכון deploy)_

- (אין חסמים)

---

## סטטוס סופי

- [x] כל הסעיפים למעלה מסומנים
- [x] אין חסמים פתוחים קריטיים

כאשר **שתי** התיבות למעלה מסומנות והמשימות בשלבים 1–3 הושלמו — הוסף מתחת לשורה הזו:

✅ הכל בוצע בהצלחה
