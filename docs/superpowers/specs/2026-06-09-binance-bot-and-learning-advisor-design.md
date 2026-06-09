# Binance Futures Bot + Learning Advisor — Design + Honest Verdict (2026-06-09)

> Produced by a 7-agent research+design+adversarial workflow against the owner's spec (`~/Downloads/BINANCE_BOT_V2_PROMPT.md`). The owner (non-technical) wants (1) a learning advisor that learns from his config/trading experiments and (2) a live-money Binance Futures tab. This is the responsible design: **build on TESTNET, prove edge before any real money — which for this strategy almost certainly never happens.**

## BOTTOM LINE (the truth the owner must hear)
The spec's bot is the **same strategy we already disproved**: 5-min BTC, bet the previous candle's direction, martingale ×2 (7 steps), 10× leverage, on a REAL account. Research (adversarially confirmed):
- **No edge** — sub-hourly BTC is ~weak-form efficient; the only measurable 5-min signal is **mean-reversion**, so previous-candle *momentum bets the wrong direction*. (Polymarket twin: 44% vs 46% fair.)
- **Costs alone make it −EV** — 0.10% taker round-trip + slippage + funding; the spec's own example shows you lose money even when you "win" at a 50% target.
- **Martingale + leverage on a no-edge process is not "risky" — it is mathematically GUARANTEED ruin**: risk grows exponentially, profit linearly; one losing streak zeroes the account, and at a ~54% loss rate a 7-loss streak hits roughly every ~160 trades (~half a day). Leverage means Binance can liquidate you *before* your own stop. This is the exact mechanism of the −85% demo blow-up — but with real money.

**One-line truth (Hebrew):** *"זאת אותה אסטרטגיה בלי יתרון שכבר הוכחנו — מרטינגייל עם מינוף עליה הוא לא 'מסוכן' אלא הפסד מתמטי מובטח שמאפס את החשבון תוך ימים; בנה את זה רק על טסטנט כדי להוכיח לעצמך, ואל תזרים כסף אמיתי."*

## What we build instead — a real, working, LEARNING platform on TESTNET
Fully-functional bot on **Binance Testnet (fake money)** with every safety control, wired to the **same learning system** that runs the Polymarket demo, so it proves with the owner's OWN numbers whether any config is +EV after real fees. Real money is gated behind that proof.

## Architecture — Option C: separate process, shared learning layer, one UI
- **Process isolation:** Binance engine = a SECOND Railway service from the same monorepo (own start cmd/Dockerfile/process) so the demo's event-loop/memory issues can never freeze a live leveraged position. (Reject the spec's standalone vanilla-JS app — it throws away the learning ask; reject same-process tab — real money mustn't share the demo's OS process.)
- **Learning reuse (owner's #1 ask):** reuse `audit_tracker.py` as-is (DATA_ROOT-keyed, engine-agnostic, has a `mode` column) → a **sidecar `binance_audit.db`** tagged `mode="binance_testnet"`. **CRITICAL:** `edge_watcher.py` does NOT filter by `mode` (verified) — pooling leveraged-futures PnL with binary-option rows = garbage; Binance must use the sidecar + a mode-scoped pass.
- **One UI:** add `BinanceTab.tsx` + `ConfigAdvisorTab.tsx` (one-line switch in `App.tsx`); UI talks to the Binance service over Railway private networking (no public port).
- **Secrets:** clone `secret_store.py` with `SERVICE="binance-futures-bot"` + `.binance_keys` (gitignored). Mirror the `POLYMARKET_LIVE` deploy kill-switch as `BINANCE_LIVE=0` default.
- **Library:** official `binance-futures-connector-python`; **stops via the Algo Order API** (`POST /fapi/v1/algoOrder`) — legacy `/fapi/v1/order` stop calls now reject `-4120` (migration effective 2025-12-09), leaving positions naked.

## Learning Advisor — the centerpiece (works for demo AND Binance)
New `engine/config_advisor.py` mirroring `edge_watcher.py` (pure, never-raises, off-loop, cached), re-aiming `edge_stats.py` (two_proportion_p, bh_fdr, deflated_sharpe, day_block_bootstrap, wilson_bounds) at **config slices** instead of feature slices. Each config = an "arm," but **NO autonomous bandit actuator — bandit theory ranks/reports, the HUMAN pulls the arm.** Honest attribution: interleaved/switchback assignment by window/day-block (reset martingale state at boundaries), regime-stratified, net of real fees, BH-FDR + Deflated-Sharpe across the number of configs tried, effective (autocorrelation-adjusted) N, anytime-valid confidence sequences (so constant peeking doesn't inflate false positives). 3-state Hebrew verdict: `לא מספיק נתונים` / `הגדרה A פחות-גרועה מ-B` / `היזהר — כל ההגדרות מפסידות`. **HARD RULE: never renders "מנצחת/profitable" while absolute net EV ≤ 0 — only "פחות גרועה"; may only ever recommend turning martingale/leverage DOWN, never up.**

## Strict phased plan (hard gates)
- **Phase 1 — Testnet build (ZERO real money):** second service, `USE_TESTNET=True`+`BINANCE_LIVE=0` (sticky). All validators as hard pre-trade gates; sidecar ledger; net-of-real-fee accounting from real fills; config_advisor + mode-scoped edge_watcher. Prove a stop ACTUALLY appears exchange-side after entry and auto-flatten if not. Gate→2: validators block bad trades, stops confirmed, ledger records net, advisor renders.
- **Phase 2 — Run testnet weeks; data decides.** No real money. Gate→3 (ALL): a config shows net-of-fee +EV ≥ +threshold, survives FDR+DSR for #configs tried, regime-stable (≥3/4 folds, ≥2/3 vol buckets), effective N≥400. **Research consensus: this gate will NOT be passed by this strategy. "Stop" is a successful outcome.**
- **Phase 3 — Real money ONLY if Phase 2 passes:** tiny capital, **martingale OFF by default** (flat sizing), **≤3× leverage** (not 10×), ISOLATED margin, exchange-native stops, restart reconciliation, hard non-resetting global equity floor.

## 6 MUST-FIX before building (from the adversarial protector-review)
1. **Live-enable must read from the LEDGER DATA, not a human-settable flag.** `BINANCE_LIVE` may only *disable*; it must NEVER be *sufficient* to enable — the engine refuses live unless the sidecar ledger contains a query-provable passing record (eff-N≥400, FDR+DSR survivor, regime-stable). (The single most important fix — closes the "owner flips the flag in 2 clicks" loophole.)
2. **Manual trading obeys the same live-gate + global equity floor** (else it's an unguarded real-money bypass of the whole gate).
3. **Fault-inject the naked-position path** (kill -9 the engine mid-entry on testnet, prove auto-flatten + reconcile-on-restart) — build-blocking, not a bullet point.
4. **Advisor never says "מנצחת/profitable" while net EV ≤ 0** — only "פחות גרועה" (the false-edge trap that would lure funding).
5. **Strike every "near/almost/likely" next to "ruin"** — it is guaranteed, not near-guaranteed.
6. **Freeze gate thresholds in code; log any change to the ledger** (loosening them = choosing to lose money).

## Non-negotiable safety/security
Testnet-first sticky; futures-trade-only keys, **withdrawals OFF**, IP-allowlisted, separate testnet/mainnet keys, keyring→chmod-600 fallback; ISOLATED margin + explicit leverage at startup (catch `-4046`); round qty DOWN to stepSize/tickSize with Decimal; read MIN_NOTIONAL **live** (Binance changed it $100→$50 on 2026-04-14 — never hardcode); two loss caps (pre-trade projected daily kill-switch + hard non-resetting global equity floor that flat-closes + disables); reconcile-on-restart ensuring every open position has a live stop; NTP clock-sync (recvWindow≤5000); ~100ms call spacing, backoff, halt on 429.

## Files (build)
Reuse as-is: `engine/audit_tracker.py` (→ sidecar), `engine/edge_stats.py`, `engine/audit_snapshot.py` (`**extra` hook). Make mode-aware: `engine/edge_watcher.py`. Clone: `engine/secret_store.py` (SERVICE="binance-futures-bot"). Mirror: `engine/main.py` `POLYMARKET_LIVE`→`BINANCE_LIVE`. Wire: `src/App.tsx` (~L4866). New: `engine/binance_engine.py`, `binance_exchange.py`, `binance_validators.py`, `config_advisor.py`, `src/BinanceTab.tsx`, `src/ConfigAdvisorTab.tsx`, second-service Dockerfile/requirements.

Full research (Binance API, strategy-edge, learning-advisor, real-money-safety, integration) + design + critic archived in the run transcript.
