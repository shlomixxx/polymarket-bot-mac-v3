# Edge-Watcher — Design Spec (v1)

> **Recording-only / advisory** module that scans the existing `audit.db` trade ledger and tells the non-technical owner, in plain Hebrew, when a *statistically genuine, tradeable* edge has emerged — so he knows when it is safe to flip the bot's decision-mode to autonomous. It never touches money, trades, config, or the hot loop. It errs **hard toward false negatives**: missing a real edge merely forgoes upside; announcing a fake edge could repeat the −85% martingale catastrophe.

Produced by a 6-agent research+design+adversarial workflow (2026-06-08). The base design and the adversarial critique are archived in the run transcript; this spec is the **reconciled** version with the critical fixes folded in.

---

## 1. Goal + hard invariants

Emit exactly one verdict state from the ledger: **`collecting` → `watching` → `forming` → `confirmed`** (renamed from the original inverted `green/red` per critique M2 — never invert traffic-light semantics in the enum). Invariants:

- **Recording-only:** zero orders, zero config writes, the autonomy switch is flipped *by the human only*. `edge_watcher.py` imports only `audit_tracker` (read) + its own stats helpers; it never imports trading code.
- **Off the event loop:** reached only via `GET /api/audit/edge` → `asyncio.to_thread(_work)` behind a 60s TTL cache (mirrors the proven `/api/audit/lessons` pattern).
- **Never raises:** every scan fn wrapped try/except → returns `[]` (mirrors `trade_coach.compute_lessons`). A bug yields a silent empty result, never a 500 or a loop stall.
- **Default-passive:** the resting state is `collecting`/`watching` ("keep watching"); any ambiguity collapses to the safer state. Uncertain ⇒ never `confirmed`.

---

## 2. What v1 measures (targets)

All targets restrict to settled, labeled rows: **`settlement_status IN ('WIN','LOSS')`** (structurally excludes the VOID/UNKNOWN/PENDING rows that fed the martingale incident).

**B — TP-REACH (PRIMARY, the money mechanic).** "In this slice, does the +18% TP actually fire more?"
- `y_tp(row) = 1 if row["exit_type"] == "TP" else 0` — **single realized definition** (critique I1: do NOT blend in the `peak>=18` near-miss counterfactual; report that only as a secondary diagnostic).

**E — ABSTENTION (SECONDARY, easiest real win).** "In this slice does TP fire *reliably less* AND do we lose money?" → a flagged slice means *skip these windows*. Economic claim must be genuinely costly: `mean(r_net|S) < −$0.10` (critique I7), not merely `<0`.

**A — DIRECTIONAL (DIAGNOSTIC-ONLY — can NEVER produce `confirmed` in v1).** `y_dir` from `cf_exit_variants.pnl_if_held_to_resolution > 0`, filtered to rows that resolved Up/Down. Reported as an info line, explicitly labeled **"(לפני עמלות אמיתיות)"** because that counterfactual is demo-fee-netted (critique C6). The 36–39% anti-predictive prior means it stays diagnostic.

**Data-path constraint (verified):** the watcher MUST read with `export_rows(labels_only=True, light=False)` — the cheap `light=True` path *discards* `context_json`/`cf_exit_variants_json`/`rule_flags_json` (`audit_tracker.py:~188`), which is exactly where the TA/CLOB features, the held-to-resolution counterfactual, and `recovery_active` live. The 60s cache + `to_thread` absorb the JSON-parse cost.

**Martingale confound guard (applies to every B/E slice).** `clean(row)` drops `rule_flags.recovery_active is True`, `loss_recovery_multiplier > 1.0`, `exploration_flag` rows. If a flagged slice's edge disappears under `clean`, it is disqualified as a martingale/side artifact.

---

## 3. The detection algorithm — with the adversarial fixes (these are load-bearing)

The original design's gates were sound in shape but had false-positive vectors. The build MUST implement the **fixed** versions below.

### 3.1 Real-fee, stake-normalized net P&L (fixes I3, I6)
The ledger's `realized_pnl` is netted at the **demo** `FEE_RATE=0.002`, not the real ~3–4% Polymarket round-trip. And under martingale, stakes vary. Therefore compute economics on **per-unit-stake, real-notional** P&L:
```
r_net(row) = (realized_pnl - (REAL_RATE - 2*FEE_RATE) * fill_price * contracts) / max(loss_recovery_multiplier, 1.0)
   REAL_RATE = 0.035 (real round-trip wedge)   FEE_RATE = 0.002 (already booked, demo)
```
Use the row's **actual fill price × contracts** (available on `light=False`), NOT a flat per-$5 wedge — a flat wedge is biased *optimistic* on cheap long-shot fills, which is exactly where the TP mechanic lives. If a future ledger books real fees, set `REAL_RATE=2*FEE_RATE`.

### 3.2 Slice-vs-complement, never slice-vs-constant (fixes C1)
Test each slice as a **two-sample** comparison (two-proportion / Fisher) against its **complement** (`rest = labeled rows NOT in slice`), with the baseline estimated on the **discovery-set complement only**, then re-confirmed slice-vs-complement on the forward sample. Never test against the global point constant `0.53` (that's circular — the slice helped define the constant).

### 3.3 Kill the i.i.d. assumption (fixes C2, I2 — the dominant false-positive vector)
Consecutive 5-min BTC windows share regime, and RSI/RV/momentum are themselves autocorrelated, so a slice is really a handful of contiguous time-blocks, not N independent draws. Therefore:
- The per-slice p-value feeding BH-FDR is a **day-block permutation/bootstrap** p-value (resample by UTC day), **not** a plain binomial.
- `N_SLICE_MIN` is expressed in **effective** sample size (post design-effect `1+(m̄−1)ρ`), landing ~**400–600 effective**, not raw 200.
- The Deflated-Sharpe winner check likewise uses day-block resampling.

### 3.4 Honest multiplicity count (fixes C3)
`m` = number of distinct **(feature, bucket, tail-direction, target)** tuples evaluated **in a single scan** — counting every categorical level, both tail directions (B upper + E lower), and **both** announce-eligible targets. Realistically `m ≈ 300–500`. **BH-FDR at q = 0.10**, applied **per-scan** (not cumulative — cumulative across 60s re-runs would either never fire or leak; see 3.5).

### 3.5 Forward-time persistence across re-scans (fixes C4, C5 — the live-safety mechanism)
A single lucky 60s scan must NOT be able to print `confirmed`. Once a slice becomes a *candidate*, **freeze the current max `decision_ts`** and require it to keep passing all gates on **trades that settle strictly after that timestamp** — i.e. true forward out-of-sample in real time. Require **≥3 consecutive confirmations spaced by ≥N additional settled trades** (tracked in an `edge_verdicts.consecutive_confirmations` counter). The "most-recent 30% vault" is anchored to this frozen wall-clock timestamp, never re-sliced from a growing table on each scan.

### 3.6 Economic gate = the master gate (with tail constraint, fix I4)
```
mean(r_net|S) >= +$0.10 per unit stake   AND   mean(r_net|S) > 0   (required even if stats pass)
AND 5th-pct of day-block-bootstrapped mean(r_net) > 0   (tail-aware, not mean-only)
AND the slice contains >= a minimum number of observed settle-losers (tail is actually sampled)
```
A slice can be p<0.001 on TP-rate and still lose money via the uncapped settlement-loss tail (TP wins cap ≈+$0.90; held losers run to −$5). **Net dollars is the verdict; TP-hit-rate is only the mechanism.**

### 3.7 Regime stability
Sign holds in ≥3 of 4 contiguous calendar folds; worst-fold `mean(r_net) > −$0.05`; top single UTC day < 40% of slice P&L; edge positive in ≥2 of 3 vol-buckets (high-vol-only edges are reported *conditional*, never an unconditional `confirmed`).

### 3.8 Final decision rule (state machine, server-side)
```
confirmed  IFF a B- or E-slice passes ALL:
  G0 data:        total_labeled >= TOTAL_MIN; effective n_in_slice >= N_SLICE_MIN; fire_rate >= 5%
  G1 forward-OOS: passes slice-vs-complement on trades after the frozen timestamp
  G2 multiplicity:survives per-scan BH-FDR q=0.10 over honest m; day-block DSR > 0.95
  G3 economic:    §3.6 master gate (mean, tail, loser-count)
  G4 stability:   §3.7
  G5 not-artifact:survives clean() martingale filter
  G6 persistence: >=3 consecutive confirmations over >=N new settled trades (§3.5)
forming    = real lift but not yet forward-confirmed / under-powered / <3 confirmations
watching   = enough data, no candidate clears even the preliminary bar
collecting = total_labeled < TOTAL_MIN
Directional (A) can only set an info line — never confirmed, never a card, never a nudge.
Ambiguity -> always return the lower (safer) state.
```

---

## 4. Architecture

- **`engine/edge_stats.py`** — pure leaves: `wilson_bounds`, `two_proportion_p`, `bh_fdr`, `deflated_sharpe`, `day_block_bootstrap`, `tertile_edges`, normal CDF/inv-CDF (no scipy). Fully unit-tested.
- **`engine/edge_watcher.py`** — `detect_edges(rows, *, config=None) -> dict`, mirrors `trade_coach.py`; never raises; thin-data → `collecting`. Internal `_scan_tp_reach`, `_scan_abstention`, `_diagnose_directional`, `_walk_forward_split`, `_bucketize` (frozen on each fold's *train segment only* — fix I5), `_evaluate_slice`.
- **`engine/main.py`** — `GET /api/audit/edge`, new `_AUDIT_EDGE_CACHE = TTLCache(60)`, wraps `detect_edges` in `asyncio.to_thread`, reads `export_rows(labels_only=True, light=False)`. Persists candidate state (frozen timestamp + `consecutive_confirmations`) in private `hypotheses` / `edge_verdicts` tables that never touch `audit_rows`.
- **`src/EdgeWatcherTab.tsx`** — new `🔭 גלאי edge` tab before `📋 ביקורת עסקאות`, styled like `AuditTab.tsx`/`FaultsTab.tsx` (RTL, 12s refresh skipping hidden). `Tab` union + tab array + render line in `App.tsx`.

**`EdgeResponse` shape:** `{ state, trades_collected, trades_min_needed, best_candidate, candidates[], directional_note_he, note }`; each `EdgeCard` = `{ setup_he, edge_type(tp_reach|abstention), hit_rate_pct, baseline_pct, lift_pct, sample_n, net_dollars_per_trade (stake-normalized real-fee), oos_confirmed, confirmations, confidence, more_trades_to_confirm }`. Add explicit `trades_min_needed_in_slice` (fix M1). Progress denominator reflects the *effective* requirement `N_SLICE_MIN / FIRE_RATE_MIN` or shows two bars (fix M4).

---

## 5. UI — three states, plain Hebrew, the button NAVIGATES (never flips)

- **collecting / watching** — calm, no button: "אין עדיין edge מובהק — ממשיכים לאסוף נתונים. הכול תקין… אל תפעיל אוטונומיה עכשיו — אין מה להפעיל." + honest progress bar.
- **forming** — "סימן מקדים ל-edge — עדיין לא מאושר. אל תפעל על סמך זה." plain-language what-we-saw + "צריך עוד ~N עסקאות + מבחן קדימה שטרם עבר." Cards show `נמוך`/`בינוני` + `🧪 טרם אומת ✗`.
- **confirmed** — the ONLY state with a button. Softened headline (fix M3): **"סימן ל-edge שעבר את כל הבדיקות — שקול להפעיל אוטונומיה"**, one plain sentence of evidence (hit-rate vs baseline, lift, N, net $/trade after real fees, forward-OOS ✓, #confirmations). The button calls `setTab("strategy")` + scroll-anchors to the existing `🤖 מצב החלטה` block — it **never** calls `setDecisionMode`. Verb is always **"שקול"** (consider).
- **HonestyFooter (always):** "'edge' נחשב אמיתי רק אחרי מבחן קדימה (out-of-sample בזמן אמת), תיקון לריבוי-בדיקות, וסף רווחיות אחרי עמלות אמיתיות (~3-4%)."

**The single autonomy-nudge guard** (the only place autonomy is ever encouraged):
```tsx
const mayNudgeAutonomy =
  state === "confirmed" &&
  best_candidate?.oos_confirmed === true &&
  best_candidate?.confidence === "high" &&
  best_candidate?.edge_type !== "directional" &&
  best_candidate?.confirmations >= 3 &&
  best_candidate?.sample_n >= trades_min_needed_in_slice;
```

---

## 6. Test plan (the critical one: T2)

T1 planted edge → `confirmed`. **T2 — pure noise stays silent across a seed sweep**, AND a variant with **autocorrelated** synthetic noise (day-blocked regimes), because the live failure mode is autocorrelation that plain i.i.d. T2 would not reproduce (the critique's sharpest point). T3 min-sample/thin-data. T4 BH-FDR unit. T5 economic gate dominates significance. T6 martingale-artifact disqualified. T7 never-raises on malformed rows. T8 leak guard (tertile edges frozen on fold train-segment only). T9 directional can't nudge. T10 Wilson bounds. **T11 — persistence: a single lucky scan does NOT print `confirmed`; requires ≥3 forward confirmations.**

---

## 7. Defaults (the 3 user-facing knobs — all conservative)

1. **Min raw lift to ever announce:** +5 percentage points.
2. **Min economic edge after real costs:** +$0.10 net per unit stake (~+2%, clears the ~3–4% wedge).
3. **Total labeled trades before any slice analysis:** 800 (`collecting` below it), with the per-slice effective gate (`N_SLICE_MIN` effective ≈400–600 at ≥5% fire) being the real binding constraint surfaced honestly in the progress UI.

All three err toward silence. They can be loosened later for more (riskier) alerts.
