# BTC TA Trading System — `trend_pullback_v1` (build spec)

> A REAL technical-analysis trading system for BTC, researched (5-agent) + adversarially reviewed. Replaces the primitive "previous-candle + martingale" spec. The owner wants real-money trading with candlesticks + indicators, built properly. This is the honest, evidence-based design — **build the harmless half first (strategy + backtester + risk engine), prove it on out-of-sample history, NO live code until the gate provably passes.**

## The honest expectation (must be said plainly, verbatim, before any real money)
> "המערכת הזאת לא נבנית כדי להרוויח — היא נבנית כדי לבדוק, על היסטוריה אמיתית עם עמלות, אם בכלל יש יתרון; והתוצאה הכי סבירה, לפי כל המחקר, היא שאין — ואז התשובה הנכונה היא פשוט להחזיק BTC ולא לסחור. אם בנינו את הכלי נכון, 'לא לסחור' זה ניצחון, לא כישלון."
A real edge here is small at best (Sharpe ~1.0–1.5 *conditional on passing*); the base-rate expectation per the research is that it does NOT beat risk-adjusted buy-and-hold. Engine = trend; candles/indicators = confirmation only (candlesticks-in-isolation are ~coin-flip after fees, worse in crypto — peer-reviewed).

## Strategy: `trend_pullback_v1` (higher-timeframe trend + pullback entry + candle trigger)
- **Market:** BTCUSDT only. **Timeframes:** Daily = trend gate, 4h = setup, 1h = entry timing. **Leverage:** ≤2–3× effective, ISOLATED margin. (5-min is explicitly rejected — fees eat the move; that's where we lost −85%.)
- **Engine = time-series momentum** (the one anomaly with cross-market academic support incl. crypto; Rohrbach 2017, Moskowitz-Ooi-Pedersen). Multi-timeframe alignment is the single biggest improvement in the research (Sharpe 0.33→0.80, DD 24%→12% on BTC).
- **LONG entry (mirror for SHORT) — ALL four, each a separate config-flag so its contribution is measurable:**
  1. **Trend gate (daily+4h):** price > daily EMA200 AND daily EMA50 > EMA200.
  2. **Pullback to level (4h):** price pulled back to 4h EMA21 / prior swing-low AND RSI(14) dipped into 40–50 (healthy pullback, not breakdown). Buy the dip in an uptrend, never the high.
  3. **Candle trigger at the level (1h/4h):** one of the best-evidenced patterns, in trend direction, on the level: **Bullish Engulfing** / **Pin Bar/Hammer** (gives the tightest logical stop — under the wick) / **Morning Star**. Body must be > k·ATR; prefer above-average volume.
  4. **Liquidity filter:** liquid sessions only (US/EU overlap), not thin weekends.
- **Exit/stop/target:** stop on the exchange, atomic with entry, at the trigger-candle wick or 1.5–2×ATR (tighter). **Rule: if structural stop > 2×ATR → SKIP.** Take-profit ≥ **2:1 reward:risk** (at 2:1 you only need 1-in-3 wins to break even — the honest version of "few wins, big"). Optional partial exit at +1R → stop to breakeven → ATR trail. **NO martingale, no adding to losers, no widening stops — enforced in code.**

## Build order (adversarially mandated — stop at the gate)
**Build the HARMLESS half first, zero real-money risk:** `btc_strategy.py` + `backtester.py` + `risk_engine.py`. Run on real BTC history, show the owner real numbers vs buy-and-hold. **Do NOT write `binance_exchange.py` / live loop until the gate provably passes on OUT-OF-SAMPLE data.**

### Files
- Reuse: `engine/ta_signals.py` (already computes EMA9/21/50/200, RSI, ATR, MACD, Bollinger, Stochastic, OBV; `fetch_btc_klines` already takes `interval`), `engine/edge_stats.py` (deflated_sharpe, day_block_bootstrap, bh_fdr, wilson_bounds), `engine/audit_tracker.py` (sidecar `binance_audit.db`, `mode="binance"`).
- New: `engine/btc_strategy.py` (signal engine + candlestick detectors: bullish_engulfing/pin_bar/morning_star as pure bool fns), `engine/backtester.py` (real history + fees/slippage/funding + walk-forward + OOS hold-out + Deflated-Sharpe vs buy-and-hold), `engine/risk_engine.py` (fixed-fractional sizing, R:R/stop/leverage rejections, daily+global loss caps), then later `engine/binance_exchange.py` + `engine/btc_signal_loop.py` + `src/BtcTradingTab.tsx`.

## The 3 adversarial MUST-FIXES (folded in)
1. **Walk-forward / OOS is a BUILD-BLOCKING gate.** Tune on data ending before a hard-frozen, never-touched-in-dev hold-out (last 12–18 months). Live-enable reads OOS performance, not in-sample. **`n_trials` fed to `deflated_sharpe` is AUTO-COUNTED by the backtester (every parameter combo ever run), never a human number** — else the best safety stat is a lie.
2. **Preserve the harsher honesty:** the base-rate expectation is the gate FAILS → hold BTC; "Sharpe 1.0–1.5" is conditional-on-passing best case, not expected. "Don't trade" is the successful outcome.
3. **Manual trading must obey the same risk_engine + live-gate + equity floor** — the risk engine is the ONLY path to the exchange, for bot AND human. Plus: **fault-inject the naked-position path** (kill engine mid-entry on testnet, prove auto-flatten + reconcile-on-restart) before any live.

## Safety floor (non-negotiable)
0.5% risk/trade (max 2% ever; stop sets size, not the reverse); stop always on the exchange (Algo Order API), atomic with entry; reconcile-on-restart auto-flattens any position lacking a live stop; daily −3% (flatten+block) + global −10% (halt+manual re-enable); `BINANCE_LIVE=0` default, live unlocked ONLY by a query-provable passing OOS ledger record (eff-N≥400, FDR+DSR survivor, regime-stable), never by a human-settable flag; start tiny; no martingale, no high leverage, no alts.
