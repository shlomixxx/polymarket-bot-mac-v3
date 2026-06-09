# Decision & Sizing Engine — Design + Honest Verdict (2026-06-09)

> Produced by a 7-agent research+design+adversarial workflow. The owner asked for a "system that really learns" and tells him per trade: what multiplier/stake, what target, how much risk — unbiased, aiming for high-WR+profit OR low-WR+huge-profit. This documents the full design AND the adversarial verdict: **do not build it now.**

## Bottom line (adversarially confirmed)
A sizing/decision engine is a **growth-rate optimizer, not an edge generator**. Kelly theory: **edge ≤ 0 → growth-optimal stake = 0.** The bot's chosen side wins **44.0% vs ~46.4% fair price** (live `/api/demo/accuracy`) = negative edge. **No multiplier, target, or risk setting changes the sign of EV.** The edge-watcher already returns this answer ("watching, no edge → bet 0"). Building 2 modules + a sidecar DB + an endpoint to restate it more eloquently is **motion, not progress** — and a sizing engine next to live trades is a loaded gun whose only safety is a `state=="confirmed"` flag the owner is motivated to flip. **VERDICT: don't build now.**

## The honest message for the owner
**You cannot out-predict 5-minute Bitcoin as a fee-paying taker.** The academic ceiling for sub-hour BTC direction is ~51–56% accuracy (barely above coin-flip; Reading 2020, arXiv 2503.18096), which is at/below the fee breakeven. Sizing can only GROW a real edge, never CREATE one — and there isn't one. The "high-WR+profit / low-WR+huge-profit" profiles are **outputs of having an edge**, not settings you can dial; with no edge both evaluate to "bet 0." The bot's current capped-win(+18%)/full-loss(−100%) shape is the WORST profile (−$0.14 EV/trade) — an honest engine would kill it.

## The ONE real lever: become a MAKER
The only structural change that can flip EV: **post LIMIT orders (maker) instead of taking at market.** Makers pay **0% fee** (+ a rebate) vs the ~7% round-trip taker fee. That drops the breakeven from ~50–54% back toward the fair price (no fee wedge). **This is an execution change, not a smarter brain.** The genuinely valuable next experiment is a maker-order spike: can the bot get limit fills on the 5-min BTC book, and what's the realized fill rate / effective cost as a maker? (Tension: maker conflicts with "enter every window at market" — fills aren't guaranteed.)

## If an edge ever IS confirmed — the design (ready to build then)
New `engine/decision_engine.py` (recording-only, never raises, no order-path imports), a strict CONSUMER of `edge_watcher.detect_edges` (reuses its walk-forward OOS + BH-FDR + day-block bootstrap + 3× forward-confirmation as the gate — adds NO new hypotheses). Layers: **A** calibrated-edge model (L2-logistic + Platt/Beta, fit on the discovery split only, scored on the sealed vault — Brier/reliability); **B** edge = q − price − fee; **C** sizing = quarter-Kelly × Baker–McHale shrinkage(σ²(q)) — wide error bars → stake→0; **D** target = hold-to-settle by default (TP is not an EV lever; Optional Stopping); **E** risk caps (≤2%/trade, daily-loss + drawdown breaker). Output: a Hebrew `TradeRecommendation` card (side, q, edge, multiplier, stake, target, risk) — today every field collapses to `STAND_DOWN, 0×` with `why_zero_he` naming the gate that blocked it. High-WR (favorite, tight CI → hold-to-settle) vs low-WR-convex (underpriced longshot → large payoff) emerge naturally from `q` vs `price`; both = 0 with no edge.

## Corrections the design needs (from the adversarial review)
1. **Fee is PRICE-SCALED, not flat.** Real per-side fee = `0.07·p·(1−p)` per share → as a fraction of stake = `0.07·(1−p)` per side, round-trip `≈ 0.14·(1−p)`: ~7% at a coin-flip (p=0.5), ~12% on a 0.12 longshot, ~4% on a 0.7 favorite. The edge-watcher's flat `REAL_RATE=0.072` is right at coin-flip but **underestimates longshots / overestimates favorites** — a price-scaled wedge is the honest refinement (longshots are MORE expensive, which matters for the convex profile the owner wants). [NOTE: there is a fee-measurement nuance — % of stake/notional (this framing, ~7% round-trip) vs % of $1 face (~3.5% round-trip); for Kelly/EV the % of stake is correct. Either way 44% < ~50% breakeven → stand down.]
2. **Add a calibration-decay kill-switch** (rolling-vault Brier degrades → force edge≤0). Non-stationarity is how a stale "confirmed" goes false.
3. **Disable the martingale UNCONDITIONALLY** (not just under autonomy) — it caused the −85% incident.
4. Soften "efficient → never confirmable": 5-min shows *some* inefficiency, but the predictable component is smaller than the fee.

## What to actually do now
- ✅ Already shipped: `/api/demo/accuracy` (true 44% vs 72% TP) + `/api/demo/real-fee-pnl` — the honest "why it's not winning" surface, for ~1% of the engine's effort.
- 🎯 Worth doing: a **maker-order experiment** (the only EV-sign lever).
- ❌ Don't build `decision_engine.py` until BOTH the maker route shows real fills AND the edge-watcher independently reaches `state="confirmed"` with `edge_ci_low > 0`. Until then it's a more articulate way to print "0."

Full research (sizing/Kelly, edge-estimation/calibration, optimal-target/convex, online-ML, prediction-market-alpha) + design + critic archived in the run transcript.
