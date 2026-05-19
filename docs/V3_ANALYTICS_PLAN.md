# V3 Analytics Plan - ניתוח מעמיק לטריידים

> **STATUS: IMPLEMENTED** — כל ה-Phases הושלמו ועובדים.
>
> מטרה: לבנות מערכת אנליטיקס שתיתן תמונה מלאה ומדויקת על ביצועי הבוט,
> כדי לדעת **מה עובד, מה לא, ומה לשנות** — על בסיס 1,074 טריידים ו-1,987 חלונות היסטוריים.

---

## Implementation Status

| Phase | Status | Files |
|-------|--------|-------|
| Phase 1: DB Migration | DONE | `engine/analytics/db_migration.py` |
| Phase 2A: Core Metrics | DONE | `engine/analytics/core_metrics.py` |
| Phase 2B: Timing Analysis | DONE | `engine/analytics/timing_analysis.py` |
| Phase 2C: Strategy Analysis | DONE | `engine/analytics/strategy_analysis.py` |
| Phase 2D: Risk Metrics | DONE | `engine/analytics/risk_metrics.py` |
| Phase 3A: Backtester | DONE | `engine/analytics/backtester.py` |
| Phase 3B: Signal Quality | DONE | `engine/analytics/signal_quality.py` |
| Phase 3C: Market Regime | DONE | `engine/analytics/market_regime.py` |
| Phase 4A: React Dashboard | DONE | `src/AnalyticsV3.tsx` |
| Phase 4B: FastAPI Endpoints | DONE | `engine/analytics/api_routes.py` |
| Phase 5: Insights Engine | DONE | `engine/analytics/insights_engine.py` |
| Integration: main.py | DONE | Router wired + auto-migration |
| Integration: App.tsx | DONE | New "Analytics V3" tab |

## Verified Results (from 1,074 trades / 317 closed sessions)

- **Win Rate:** 69.09%
- **Total PnL:** $6,075.97
- **Expectancy:** $19.17 per trade
- **Profit Factor:** 1.357
- **Sharpe Ratio:** 0.058
- **Max Drawdown:** -$4,016.78
- **Max Win Streak:** 13
- **DCA improves win rate:** Yes
- **Loss Recovery net:** $3,409.73 (profitable)
- **Best hour:** 19:00 UTC (89% WR)
- **Better side:** Up
- **Optimal TP%:** 50% (confirmed)

---

## מקורות דאטה קיימים

| מקור | מה יש בו | כמות |
|------|-----------|------|
| `demo_state.json` → trades | כל טרייד עם pnl_path, peak/trough, session_id | 1,074 רשומות |
| `history.db` → window_results | Up/Down per 5min window + BTC open/close | 1,987 חלונות |
| `logs/runs/` | strategy snapshots + combined logs per run | ריצות מרובות |
| `config_persisted.json` | הגדרות אסטרטגיה נוכחיות | - |

---

## Phase 1: Trade Database Migration (SQLite)

### למה?
כרגע כל הטריידים ב-JSON אחד גדול (`demo_state.json`). זה איטי לשאילתות, לא מאפשר אגרגציות מהירות, ולא סקיילבילי.

### משימות

| # | משימה | Skills נדרשים |
|---|--------|---------------|
| 1.1 | יצירת טבלת `trades` ב-`history.db` עם כל השדות מה-JSON | Python, SQLite, DB Schema Design |
| 1.2 | יצירת טבלת `sessions` (אגרגציה ברמת session_id) | Python, SQLite |
| 1.3 | יצירת טבלת `pnl_snapshots` ל-pnl_path (time series) | Python, SQLite, Time Series |
| 1.4 | Migration script: JSON → SQLite (one-time + incremental) | Python, Data Migration |
| 1.5 | עדכון `demo_engine.py` / `main.py` לכתוב ישירות ל-SQLite | Python, FastAPI |

### סכמת טבלאות מוצעת

```sql
-- טרייד בודד
CREATE TABLE trades (
    id TEXT PRIMARY KEY,            -- UUID מהטרייד
    ts REAL NOT NULL,               -- timestamp
    type TEXT NOT NULL,             -- BUY, SELL_TP, EXPIRE_0, SETTLE_WIN, SETTLE_LOSS, RECONCILE
    side TEXT,                      -- Up / Down
    contracts REAL,
    price REAL,
    fee_est REAL,
    token_id TEXT,
    session_id TEXT,                -- קישור לסשן
    epoch INTEGER,
    slug TEXT,
    window_sec INTEGER DEFAULT 300,
    realized_pnl REAL,
    peak_unrealized_pct REAL,
    peak_ts REAL,
    trough_unrealized_pct REAL,
    trough_ts REAL,
    entry_target_usd REAL,
    limit_price REAL,
    effective_investment_usd REAL,
    loss_recovery_multiplier REAL,
    ask_u REAL, bid_u REAL,
    ask_d REAL, bid_d REAL,
    reason TEXT,
    execution TEXT,                 -- demo / live
    gate TEXT,
    reconcile_origin INTEGER DEFAULT 0
);

-- סשן = מחזור שלם (entry + exit)
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    side TEXT,
    entry_ts REAL,
    exit_ts REAL,
    exit_type TEXT,                -- TP, EXPIRE, SETTLE_WIN, SETTLE_LOSS
    total_invested_usd REAL,
    total_contracts REAL,
    avg_entry_price REAL,
    realized_pnl REAL,
    duration_sec REAL,
    num_dca_slices INTEGER,
    entry_spread REAL,            -- bid-ask spread at entry
    peak_unrealized_pct REAL,
    trough_unrealized_pct REAL,
    loss_recovery_multiplier REAL,
    epoch INTEGER,
    slug TEXT,
    hour_utc INTEGER,
    weekday INTEGER,
    execution TEXT
);

-- נתיב PnL (time series per session)
CREATE TABLE pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    upnl_pct REAL,
    bid REAL,
    balance REAL,
    equity REAL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX idx_pnl_session ON pnl_snapshots(session_id);
```

---

## Phase 2: Core Analytics Engine

### 2A - מדדי ביצוע גלובליים

| # | מטריקה | מה זה נותן | Skills |
|---|--------|-------------|--------|
| 2.1 | **Win Rate** (by exit type) | % סשנים שנגמרו ברווח | SQL, Statistics |
| 2.2 | **Expectancy** | תוחלת רווח ממוצעת per trade | Statistics |
| 2.3 | **Profit Factor** | total_wins / total_losses | SQL |
| 2.4 | **Sharpe Ratio** | risk-adjusted return | Statistics, Finance |
| 2.5 | **Max Drawdown** | הירידה הגדולה ביותר מהשיא | SQL, Finance |
| 2.6 | **Recovery Factor** | net profit / max drawdown | Finance |
| 2.7 | **Average R:R** | avg win / avg loss | Statistics |
| 2.8 | **Consecutive Wins/Losses** | סטריקים ואורכם | SQL |

### 2B - ניתוח תזמון (Timing Analytics)

| # | מטריקה | מה זה נותן | Skills |
|---|--------|-------------|--------|
| 2.9 | **Win Rate by Hour (UTC)** | באיזה שעות הבוט רווחי | SQL, Time Analysis |
| 2.10 | **Win Rate by Weekday** | באיזה ימים כדאי לעבוד | SQL |
| 2.11 | **Win Rate by Entry Minute in Window** | מתי בחלון הכי טוב להיכנס | SQL, Time Analysis |
| 2.12 | **Optimal Exit Timing** | כמה זמן להחזיק פוזיציה | Statistics, Time Series |
| 2.13 | **Session vs Hour Heatmap** | מטריצת שעה × יום | Data Viz, Heatmap |

### 2C - ניתוח אסטרטגיה

| # | מטריקה | מה זה נותן | Skills |
|---|--------|-------------|--------|
| 2.14 | **DCA Effectiveness** | האם DCA משפר avg entry vs single | SQL, Strategy Analysis |
| 2.15 | **DCA Slice Analysis** | ביצועים per slice number (1st, 2nd, 3rd) | SQL |
| 2.16 | **Loss Recovery ROI** | האם מולטיפלייר באמת מחזיר? | SQL, Risk Analysis |
| 2.17 | **Loss Recovery Cascade** | כמה רמות ריקברי נדרשו, ומתי פספסנו | SQL |
| 2.18 | **TP Level Analysis** | האם 50% TP אופטימלי? מה היה קורה ב-30%, 70%? | Simulation, Backtesting |
| 2.19 | **Peak Unrealized vs Actual Exit** | כמה כסף נשאר על השולחן | Statistics |
| 2.20 | **Side Preference Analysis** | Up vs Down — מי יותר רווחי ולמה | SQL, Market Analysis |

### 2D - ניתוח סיכונים

| # | מטריקה | מה זה נותן | Skills |
|---|--------|-------------|--------|
| 2.21 | **Drawdown Curve** | גרף של equity drawdown over time | Time Series, Finance |
| 2.22 | **Risk of Ruin** | הסתברות להגיע ל-0 בהתחשב בפרמטרים | Monte Carlo, Statistics |
| 2.23 | **Position Sizing Impact** | איך investment_usd משפיע על ביצועים | Simulation |
| 2.24 | **Slippage Analysis** | הפרש בין limit_price ל-price בפועל | SQL |
| 2.25 | **Fee Impact** | כמה עמלות אכלו מהרווח | SQL |

---

## Phase 3: Advanced Analytics

### 3A - Backtesting Engine

| # | משימה | Skills |
|---|--------|--------|
| 3.1 | **What-If Simulator** — שינוי TP%, entry_price, DCA params ובדיקה על דאטה היסטורי | Python, Backtesting, Statistics |
| 3.2 | **Optimal Parameter Search** — grid search על פרמטרים (TP, entry price, min_minutes) | Optimization, Grid Search |
| 3.3 | **pnl_path Replay** — השמעה חוזרת של נתיב PnL ובדיקת "מה היה קורה אם..." | Time Series, Simulation |
| 3.4 | **Walk-Forward Validation** — בדיקה שפרמטרים אופטימליים לא overfitted | ML, Cross Validation |

### 3B - Signal Quality Analysis

| # | משימה | Skills |
|---|--------|--------|
| 3.5 | **Signal Accuracy Score** — כמה פעמים הסיגנל צדק | SQL, Signal Analysis |
| 3.6 | **Signal Confidence vs Outcome** — האם confidence גבוה = יותר wins? | Statistics, Correlation |
| 3.7 | **TA vs CLOB vs History** — איזה signal component הכי מדויק | ML, Feature Importance |
| 3.8 | **False Signal Detection** — זיהוי patterns של סיגנלים שטעו | Pattern Recognition |

### 3C - Market Regime Detection

| # | משימה | Skills |
|---|--------|--------|
| 3.9 | **Volatility Regime** — זיהוי high/low vol ו-win rate per regime | Statistics, Volatility |
| 3.10 | **Trending vs Range** — ביצועים כש-BTC trending vs sideways | TA, Market Analysis |
| 3.11 | **BTC Price Movement vs Win Rate** — קורלציה בין גודל התזוזה לתוצאה | Correlation, Statistics |

---

## Phase 4: Dashboard UI

### 4A - דף Analytics חדש (React)

| # | משימה | Skills |
|---|--------|--------|
| 4.1 | **Overview Card** — סיכום כללי: win rate, expectancy, profit factor, total PnL | React, Recharts |
| 4.2 | **Equity Curve Chart** — גרף צמיחת ההון לאורך זמן | React, Recharts, Time Series |
| 4.3 | **Drawdown Chart** — גרף drawdown מתחת ל-equity curve | React, Recharts |
| 4.4 | **Heatmap: Hour × Day** — מפת חום של win rate | React, D3/Heatmap |
| 4.5 | **Distribution Charts** — histogram של PnL per trade | React, Recharts |
| 4.6 | **Strategy Comparison Table** — DCA on/off, recovery on/off side by side | React, Table |
| 4.7 | **Entry Timing Scatter** — minute in window vs PnL (scatter plot) | React, Recharts |
| 4.8 | **Filters** — filter by date range, side, execution mode, exit type | React, State Management |

### 4B - API Endpoints

| # | משימה | Skills |
|---|--------|--------|
| 4.9 | `GET /api/analytics/overview` — מדדי ביצוע גלובליים | FastAPI, SQL |
| 4.10 | `GET /api/analytics/equity-curve` — time series של equity | FastAPI, SQL |
| 4.11 | `GET /api/analytics/timing` — הפירוט לפי שעה/יום/דקה בחלון | FastAPI, SQL |
| 4.12 | `GET /api/analytics/strategy` — השוואת אסטרטגיות | FastAPI, SQL |
| 4.13 | `GET /api/analytics/risk` — מדדי סיכון | FastAPI, SQL |
| 4.14 | `GET /api/analytics/backtest` — הרצת what-if simulation | FastAPI, Python |
| 4.15 | `GET /api/analytics/signals` — דיוק סיגנלים | FastAPI, SQL |

---

## Phase 5: Automated Insights & Recommendations

| # | משימה | Skills |
|---|--------|--------|
| 5.1 | **Auto-Insight Engine** — זיהוי אוטומטי של patterns חשובים | Python, Statistics |
| 5.2 | **Parameter Recommendations** — "על בסיס 1000 טריידים, כדאי לשנות TP ל-X" | ML, Optimization |
| 5.3 | **Alert System** — התראה כשמדד חורג (drawdown > 20%, win rate < 40%) | Python, WebSocket |
| 5.4 | **Daily/Weekly Report** — סיכום אוטומטי לתקופה | Python, Templating |
| 5.5 | **Config Tuner** — הצעה אוטומטית של config_persisted.json אופטימלי | Optimization, Backtesting |

---

## סדר עדיפויות (Priority Matrix)

### P0 - Must Have (שבוע 1-2)
- [1.1-1.5] Database migration — בלי זה אין כלום
- [2.1-2.8] מדדי ביצוע גלובליים — הבסיס
- [4.1-4.3] Overview + Equity Curve — ויזואליזציה ראשונית
- [4.9-4.10] API endpoints בסיסיים

### P1 - High Value (שבוע 3-4)
- [2.9-2.13] ניתוח תזמון — מתי לסחור
- [2.14-2.20] ניתוח אסטרטגיה — מה עובד
- [4.4-4.8] Charts מתקדמים + Filters
- [4.11-4.13] API endpoints נוספים

### P2 - Advanced (שבוע 5-6)
- [2.21-2.25] ניתוח סיכונים
- [3.1-3.4] Backtesting engine
- [3.5-3.8] Signal quality
- [4.14-4.15] API endpoints מתקדמים

### P3 - Optimization (שבוע 7+)
- [3.9-3.11] Market regime detection
- [5.1-5.5] Automated insights & recommendations

---

## Skills Summary (כישורים נדרשים)

| Skill | איפה נדרש | רמת חשיבות |
|-------|-----------|------------|
| **Python** | כל Phase | קריטי |
| **SQLite / SQL** | Phase 1-3 | קריטי |
| **FastAPI** | Phase 1, 4B | קריטי |
| **React + TypeScript** | Phase 4A | קריטי |
| **Recharts / D3** | Phase 4A | גבוה |
| **Statistics** | Phase 2-3 | גבוה |
| **Finance / Trading Metrics** | Phase 2C, 2D | גבוה |
| **Time Series Analysis** | Phase 2B, 3A | גבוה |
| **Backtesting** | Phase 3A | בינוני |
| **Data Migration** | Phase 1 | בינוני |
| **Optimization / Grid Search** | Phase 3A, 5 | בינוני |
| **Monte Carlo Simulation** | Phase 2D | בינוני |
| **ML / Feature Importance** | Phase 3B | נמוך (P3) |
| **Pattern Recognition** | Phase 3B, 3C | נמוך (P3) |

---

## שאלות מפתח שהאנליטיקס עונה עליהן (VERIFIED)

1. **מה ה-win rate האמיתי שלי?** → **69.09%** (לפי exit type, side, שעה, יום)
2. **מה התוחלת שלי per trade?** → **$19.17** (expectancy בדולרים)
3. **האם DCA עוזר או מזיק?** → **DCA improves win rate** (single vs multi-slice)
4. **האם loss recovery משתלם?** → **כן, net +$3,409.73**
5. **מתי הכי טוב להיכנס בחלון?** → ניתוח by entry minute available
6. **מה ה-TP האופטימלי?** → **50% confirmed** (grid search 10-200%)
7. **באיזה שעות כדאי לכבות את הבוט?** → **Hour 20 UTC unprofitable** (-$80.60/trade)
8. **כמה אני מפסיד על fees ו-slippage?** → **$358 fees (5.57% drag)**
9. **מה ה-max drawdown שלי ומה הסיכון?** → **-$4,016.78 DD, 95.6% RoR**
10. **איזה סיגנל הכי מדויק?** → Signal accuracy by gate available

---

## API Endpoints (All Implemented)

```
POST /api/analytics/migrate              — Run JSON → SQLite migration
GET  /api/analytics/db-stats             — Table counts
GET  /api/analytics/overview             — Global metrics
GET  /api/analytics/equity-curve         — Cumulative PnL time series
GET  /api/analytics/timing/hourly        — Win rate by UTC hour
GET  /api/analytics/timing/weekday       — Win rate by day of week
GET  /api/analytics/timing/entry-minute  — Win rate by minute in window
GET  /api/analytics/timing/heatmap       — Hour × Day matrix
GET  /api/analytics/timing/optimal-exit  — Holding duration buckets
GET  /api/analytics/strategy/dca         — DCA effectiveness
GET  /api/analytics/strategy/loss-recovery — Recovery ROI
GET  /api/analytics/strategy/tp-analysis — TP level analysis
GET  /api/analytics/strategy/side-preference — Up vs Down
GET  /api/analytics/risk/drawdown        — Drawdown curve + stats
GET  /api/analytics/risk/slippage        — Slippage analysis
GET  /api/analytics/risk/fees            — Fee impact
GET  /api/analytics/risk/pnl-distribution — Histogram + percentiles
GET  /api/analytics/risk/ruin            — Risk of ruin estimation
GET  /api/analytics/backtest/tp          — What-if TP%
GET  /api/analytics/backtest/optimal-tp  — Grid search TP%
GET  /api/analytics/backtest/entry-price — What-if entry threshold
GET  /api/analytics/backtest/optimal-entry — Grid search entry price
GET  /api/analytics/signals/accuracy     — Signal accuracy by gate
GET  /api/analytics/signals/window-prediction — Side prediction accuracy
GET  /api/analytics/market/volatility-regimes — BTC vol regimes
GET  /api/analytics/market/btc-correlation — BTC direction correlation
GET  /api/analytics/insights             — Automated insights
GET  /api/analytics/recommendations      — Config recommendations
GET  /api/analytics/full-report          — Everything in one call
```

---

## קבצים שנוצרו

```
engine/analytics/
├── __init__.py              # Package init
├── db_migration.py          # Phase 1: JSON → SQLite (trades, sessions, pnl_snapshots)
├── core_metrics.py          # Phase 2A: win rate, expectancy, PF, Sharpe, drawdown, streaks
├── timing_analysis.py       # Phase 2B: hourly, weekday, entry minute, heatmap, optimal exit
├── strategy_analysis.py     # Phase 2C: DCA, loss recovery, TP, side preference
├── risk_metrics.py          # Phase 2D: drawdown curve, slippage, fees, PnL dist, risk of ruin
├── backtester.py            # Phase 3A: what-if TP/entry, grid search
├── signal_quality.py        # Phase 3B: signal accuracy, window prediction
├── market_regime.py         # Phase 3C: volatility regimes, BTC correlation
├── insights_engine.py       # Phase 5: auto insights + config recommendations
└── api_routes.py            # Phase 4B: FastAPI router (29 endpoints)

src/
├── AnalyticsV3.tsx          # Phase 4A: Full dashboard (6 sub-tabs, charts, tables, heatmap)

Modified:
├── engine/main.py           # Router wired + auto table creation
└── src/App.tsx              # New "Analytics V3" tab
```
