# בוט Polymarket — BTC Up/Down (גירסה v2)

פרויקט **v2** הוא עותק מפותח של `polymarket-bot-mac` (v1) עם **ממשק משודרג** (טוקנים, `Card` / `ChartCard`, טאבים, גרפים מאוחדים), **אנימציית גרפים מותנית** (פחות עומס CPU ברענון תכוף), ו־**Electron** עם מינימום חלון. לוגיקת המנוע ב־Python **זהה ברובה** ל־v1.

## מה השתנה למשתמש (v2 לעומת v1)

| נושא | v1 (`polymarket-bot-mac`) | v2 (`polymarket-bot-mac-v2`) |
|------|---------------------------|-------------------------------|
| מראה | סגנונות מפוזרים ב־`App.tsx` | שכבת shell, כרטיסים, טאבים עם `data-active`, צבעי surface מדורגים |
| גרפים | Recharts ad-hoc | `chartConstants` + Tooltip/צירים אחידים; מסלול תשואה ב־`monotone`; PnL מצטבר עם `smoothCurveType` |
| ביצועים | אנימציה בכל רענון | `useChartAnimationGate` — אנימציה כשהאורך/הערך האחרון משתנה משמעותית |
| פורטים | Vite 5173 / מנוע 8765 (ברירת v1) | **Vite 5174** / **מנוע 8766** — להרצה מקבילה עם v1 |
| חלון | ללא מינימום מוגדר בקוד המצורף ב־v1 | `minWidth` 1024px, `minHeight` 680px |

## דרישות

- macOS  
- Python 3.10+  
- Node.js 18+

## התקנה

### מנוע (Python)

במק נפוצים **שני Pythonים**: למשל אחד מ־**python.org** (`/Library/Frameworks/...`) ואחד מ־**Homebrew** (`/opt/homebrew/...`).  
`pip` עלול להתקין ל־אחד בעוד ש־`python3` בטרמינל מצביע על השני — ואז מופיע `No module named uvicorn`.  
הפרויקט **פותר אוטומטית** Python שבו מותקן `uvicorn` (עדיפות ל־Frameworks), ומריץ את המנוע עם אותו נתיב.

**בלחיצה כפולה (מומלץ):** בתיקיית הפרויקט הרץ פעם אחת את  
**`install-engine-deps.command`**  
(מתקין לפי אותו סדר עדיפות), ואז **`run-bot-with-logs.command`**.

**או מהטרמינל:**

```bash
cd "/Users/shlomishemtov/Documents/cursor project/polymarket-bot-mac-v2/engine"
python3 -m pip install -r requirements.txt
```

(מומלץ `python3 -m pip` ולא רק `pip3`, כדי שהחבילות ייכנסו לאותו Python.)

### ממשק (Node)

```bash
cd "/Users/shlomishemtov/Documents/cursor project/polymarket-bot-mac-v2"
npm install
```

## הרצה בפיתוח

פקודה אחת (מנוע + Vite + Electron):

```bash
cd "/Users/shlomishemtov/Documents/cursor project/polymarket-bot-mac-v2"
npm run dev
```

- UI: `http://127.0.0.1:5174`  
- API מנוע: `http://127.0.0.1:8766`

### הרצה מקבילה של v1 ו־v2

- השארו ב־v1 את הפורטים המקוריים; ב־v2 משתמשים ב־**5174** ו־**8766** כדי שלא יהיו התנגשויות.  
- הריצו כל פרויקט ממחיצת הפרויקט שלו (`npm run dev`).

### לחיצה כפולה על `run-bot-with-logs.command` — נפתח רק טרמינל בלי חלון Electron

ב־macOS, הפעלה מ־Finder נותנת לעיתים **PATH קצר** — בלי `node`/`npm` הסקריפט נעצר, או ש־**Electron** מחכה לנצח כי המנוע/Vite לא עלו.

- **`run-bot-with-logs.command`** מריץ את `run-with-logs.sh` דרך **`zsh -l` (login shell)** כדי לטעון את `.zprofile` / כלים כמו **fnm / Volta / asdf / nvm** כמו בטרמינל רגיל.
- `scripts/run-with-logs.sh` מוסיף גם נתיבי Homebrew ו־nvm/fnm/volta/asdf; בתחילת הריצה מודפסים נתיבי `node`/`npm`.
- **`dev:electron`** משתמש ב־`wait-on` עם **timeout (3 דקות)** ו־**verbose** — אם אחרי 3 דקות אין מנוע על `8766` או Vite על `5174`, תראה שגיאת timeout בטרמינל (ולא המתנה אינסופית).
- אם עדיין אין חלון — פתח את **`logs/runs/.../combined.log`** באותה ריצה וחפש `Error`, `wait-on`, או `electron`.
- ודא שביצעת `npm install` ו־`pip3 install -r requirements.txt` במנוע.
- **פורט תפוס** (`8766` / `5174`) — סגור ריצה ישנה (Ctrl+C) או תהליך אחר; אל תלחץ פעמיים מהר על `.command`.

## בניית Web (אימות ייצור)

```bash
npm run build:web
```

פלט ב־`dist/`.

## בדיקה ידנית לפני סגירה (QA קצרה)

1. **דשבורד** — טעינה, גרף BTC, מחיר לנצח, יתרה/איפוס דמו.  
2. **אסטרטגיה** — פריסטים, שמירה, מצב בוט (כבוי/חצי/אוטו).  
3. **סטטיסטיקה** — גרף PnL מצטבר, טבלת היסטוריה, הרחבת עסקה + גרף מסלול תשואה.  
4. **ספר לא עדכני** (אם מופיע) — הודעת אזהרה, גרף לא נשבר.  
5. **RTL** — כותרות טבלה וטקסט קריאים.  
6. **חלון צר** — מעל המינימום — כפתורים וטבלאות לא נחתכים קריטית.

## מקור מחירי BTC — דיוק מלא מול Polymarket

שוקי "BTC Up or Down 5m" ב-Polymarket **נסגרים לפי Chainlink BTC/USD Data Stream**
(`resolutionSource: data.chain.link/streams/btc-usd`). כדי שהמספרים במכון יהיו זהים
*בדיוק* לאתר, גם ה**מחיר הנוכחי** וגם ה**Price to Beat** נלכדים מאותו פיד:

- `engine/chainlink_price_stream.py` — חיבור WebSocket מתמשך ל-`wss://ws-live-data.polymarket.com`,
  topic `crypto_prices_chainlink`. הפיד עונה ל-`subscribe` ב-snapshot חד-פעמי של ~60ש׳ טיקים
  (1Hz), **ללא push** — לכן אנחנו **re-subscribe** כל שנייה כדי לרענן, עם PING כל 5ש׳,
  reconnect עם backoff ו-watchdog ל-stale (חיקוי הדפוס של `ws_price_stream.py`, אך חיבור נפרד).
  - **מחיר נוכחי** = הטיק הטרי ביותר (`get_current_price`).
  - **Price to Beat** = הטיק הראשון שחותמתו ≥ `window_start` (`get_price_to_beat`), נשמר
    immutable פר-חלון. אם המכון עולה **באמצע חלון** ואין כיסוי לפני הגבול — מוחזר `None`
    (לא ממציאים מספר), והחלון הבא נלכד במדויק.
- **שרשרת fallback** (רק כשפיד Chainlink לא זמין/טרי): מחיר נוכחי → Binance spot; Price to Beat
  → אורקל Chainlink על Polygon → נר Binance 1m. המקור מסומן תמיד: `chainlink_stream`
  (מדויק) לעומת `binance_fallback` / `chainlink_polygon_window` / `binance_1m_fallback`.
- ההחלטות האסטרטגיות רצות על מחיר Chainlink (ראה `trigger_engine._fetch_btc_price`,
  `btc_price.fetch_btc_current_usd`). ה-endpoints `/api/btc/live` ו-`/api/market/current`
  מחזירים את המקור (`source` / `price_to_beat_source`) וה-`price_to_beat_note` משקף אותו.
- **הערה:** פירוק ה-settlement בדמו עדיין משתמש בפרוקסי Binance 1m (מחוץ להיקף השינוי הזה).

## אסטרטגיית "Chop-Armed Follow-the-Winner" (opt-in)

הפחתת סיכון מרטינגייל: הבוט **מחכה** עד שיהיו N נרות 5-דק׳ מתחלפים ברצף ("דשדוש",
`chop_length_n`, למשל 4 = 🔴🟢🔴🟢), ואז נכנס **לפי המנצח האחרון** (FLW-forward) ומכפיל על
הפסד עד התקרה (`loss_recovery`, חסום קשיח ל-×3). הקמפיין הוא **אפיזודת-החלמה אחת**: ברגע שהוא
מגיע לרווח (**ניצחון**) — או אם הפסד נוחת במכפיל המקסימלי (החלמה מוצתה) — הקמפיין נגמר וחוזרים
ל-WAITING עד הדשדוש הבא. בין דשדוש לדשדוש אין כניסות. כך נמנע רצף ההפסדים שמנפח את ההכפלה.

- לוגיקה טהורה: [`engine/chop_gate.py`](engine/chop_gate.py) (`is_chop`, `campaign_should_end`).
- שער כניסה + מצב קמפיין: `_check_chop_gate` / `_update_chop_campaign` ב-[`engine/strategy_runner.py`](engine/strategy_runner.py)
  (fail-open, לעולם לא מפיל את הלולאה); כיוון נגזר מ-`get_last_window_winners` (היסטוריה על `/data`).
- הגדרות: `chop_armed_flw_enabled` (כבוי כברירת מחדל), `chop_length_n` (2-10). לוח הבקרה מציג
  תג מצב קמפיין חי + רצועת חלונות אחרונים (🔴🟢) + ניצחונות הבוט.
- עלות זניחה: קריאת SQLite מקומית אחת לחלון (כמו FLW), בלי loops/endpoints חדשים.

## מבנה תיקייה (עיקרי)

- `engine/` — FastAPI, דמו, אסטרטגיה; `chainlink_price_stream.py` (פיד המחירים המדויק)  
- `src/` — React; `ui/` רכיבים; `chartConstants.ts`  
- `electron/` — חלון, פורט dev  

## מסחר בכסף אמת (אופציונלי)

כמו v1: `py-clob-client`, מפתח ב־UI, מצב לייב. ראו גם תיעוד Polymarket ותנאי שימוש. **סיכון כספי.**

---

מדריכים נוספים (אם קיימים בפרויקט): `docs/`.
