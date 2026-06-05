# Trade Audit & Learning Ledger — Design Spec

- **Date:** 2026-06-05
- **Status:** Draft for review
- **Author:** Claude (brainstormed with Shlomi)
- **Scope of this spec:** Phase A only (the AI-ready foundation). Phases B/C are documented as roadmap.

---

## 1. Goal & Vision

**User's stated goal (verbatim intent):** *"document everything important about each trade so that in the future the AI will use this data to trade autonomously and change what's needed."*

Full autonomous self-adjustment (a closed feedback loop) is the **destination**, but it is also the most dangerous step — this project already suffered an **−85% demo loss** when an automated loop (`SETTLE_UNKNOWN` → martingale) treated a non-outcome as a real loss and doubled into the ground (see memory: *martingale-incident-and-faults-tab*). The safe path to "AI trades on its own" is to **first build a complete, machine-readable, leakage-free record of every trade** — the *fuel* a future AI needs to learn.

This spec builds **Phase A: the Trade Audit & Learning Ledger** — a per-trade record that captures the full story of each trade (the *why* at entry, *what happened*, and *what could have been better*), stored so a future AI can consume it directly. It also ships an `/api/audit/export` endpoint as the AI's entry point and a UI tab for the human to learn from today.

### Phasing
- **Phase A (this spec, build now):** The audit ledger — capture + store + UI tab + AI export endpoint. Immediate human value; AI-ready by construction.
- **Phase B (roadmap):** AI coach — Claude periodically reads `/api/audit/export` and writes plain-language lessons (recommendations only).
- **Phase C (roadmap, high-caution):** Autonomous self-adjustment — gated by hard guardrails informed by the martingale incident.

---

## 2. Background — What Exists vs. The Gap

### Already exists (do not rebuild)
- **Per-trade outcome records** in `demo_state.trades` (`engine/demo_engine.py`): `id, ts, type, side, contracts, price, fee_est, token_id, session_id, realized_pnl, peak_unrealized_pct, trough_unrealized_pct, pnl_path, settlement_btc_start/end, resolved_outcome, settlement_won, voided`, etc.
- **A full analytics suite** (`engine/analytics/`, 11 modules) surfaced in the `ניתוח v3` and `📊 אנליטיקס V3` tabs: win-rate, expectancy, Sharpe, drawdown, timing heatmaps, DCA effectiveness, loss-recovery ROI, signal quality, market regime, backtester.
- **The Faults tab pattern** (`engine/fault_tracker.py` → `faults.db` → `/api/faults` → `src/FaultsTab.tsx`) — the exact tracker→SQLite→API→tab template this feature follows.
- **History DB** (`engine/history_tracker.py` → `history.db` `window_results`): per-window Up/Down outcome, BTC open/close, hour/weekday.
- **WS-first / REST-fallback feeds** (recent perf PRs F4/PR-F) and a heavy `demo_state.json` that has been deliberately shrunk (26MB → lean; `pnl_path` capping). **The audit data must NOT re-bloat this hot state.**

### The gap this feature fills
The trade record captures **what happened** but **discards why the trade was entered.** Verified in code:
- `compute_signals()` (TA + CLOB + history + sentiment → recommendation/confidence) is called in `engine/main.py:1912` and `engine/trigger_engine.py`, **but never in `engine/strategy_runner.py`**.
- The entry context built at `engine/strategy_runner.py:1358` (`base_ctx`) contains only `epoch, slug, gate, min_left_sec, ask_u/bid_u/ask_d/bid_d, order_min_size, window_sec, btc_window` — **no signal/TA/CLOB/sentiment snapshot.**

So today you cannot ask "did the trades where the signal disagreed with CLOB lose more?" — the answer was computed and thrown away. **Closing this gap is the single highest-leverage task in this spec.**

---

## 3. Design Principles (grounded in external research)

These principles come from a research pass over professional trade-journaling, ML feature-store / point-in-time-correctness, offline-RL/label-design, and data-pitfall literature (sources in §13). Each is filtered for *our* scope: a tiny-order binary BTC up/down 5m/15m prediction-market bot whose data must fuel a future Claude AI.

1. **Capture the WHY point-in-time, inline at the decision tick.** Write the exact signal/feature values the bot used at the moment of decision; never reconstruct them later from current data (lookahead-bias / training-serving-skew). *This requires wiring `compute_signals` into the entry path — the core engine change.*
2. **Decision-time fields are immutable; settlement appends only.** Two knowledge-times, two timestamp sets: `decision_ts` (written once) and `settled_ts` (written at resolution), with invariant `settled_ts > decision_ts`. Settlement code must never overwrite a decision-time field. (Today `demo_engine` mutates the BUY dict to bolt on `settlement_btc_*` — the audit ledger must not inherit that.)
3. **`settlement_status` is a first-class enum, separate from PnL.** `{WIN, LOSS, VOID, INVALID, UNKNOWN, PENDING}`, default `PENDING` (never 0). **Both** the training export **and** the martingale must quarantine anything not in `{WIN, LOSS}`. This is the same enum that closes the documented incident loop.
4. **Version every row.** `schema_version` (monotonic int) + `code_version` (git SHA). Schema changes are additive/append-only (add nullable columns; never repurpose a field's meaning). Lets a future AI union old + new eras without conflating drifted columns — essential because Layer B is being filled in incrementally.
5. **Exploit binary-market counterfactuals (nearly free).** Once a window resolves, the unchosen leg's payoff is fully determined. Log `cf_other_side_pnl` and `cf_exit_variants` (held-to-resolution / peak / trough) derived from data we already capture (`pnl_path`, watermarks, settlement BTC). Turns one-outcome bandit logs into near-full-feedback supervision.
6. **Snapshot the policy that generated the row.** `policy_id` + `policy_params_json` (TP, price cap, order_mode, side_preference, and critically `loss_recovery_multiplier` + `loss_recovery_streak`). Two rows with identical signals but different sizing are otherwise unexplainable to a learner.
7. **Log data provenance + freshness for decision-driving feeds.** `btc_spot_source` (ws|rest) + `age_ms`, `book_source` + `age_ms`, `signals_stale`/`signals_missing`. Directly relevant to our WS-first/REST-fallback architecture — distinguishes a bad fill from stale-data-at-decision.
8. **Stamp regime context at decision time.** `vol_bucket`, `btc_change_pct_at_entry`, `seconds_remaining_at_entry`, `entry_minute_in_window` — enables regime-stratified / walk-forward analysis instead of random shuffles.
9. **Anti-survivorship: append-only, log attempts too.** Record rejected/no-fill/expired entry attempts (not just filled trades) with an `outcome_reason`. Never delete rows, only supersede. (Phase A: filled + settled is the priority; attempt-logging is a thin add documented in §7.)
10. **One canonical clock: UTC epoch-ms.** Convert to local only for display.

### Deliberately OUT of scope (anti-cargo-cult, with rationale)
- **Full institutional TCA** (VWAP/TWAP, participation rate, market impact, maker/taker): for large orders worked over time against deep books. Our orders are single-tick tiny binary entries on a thin book. *Only `arrival_price` + `entry_slippage` map; the rest is noise.*
- **R-multiples & MAE/MFE-in-R:** assume a stop-defined risk per trade. Our instrument is binary (max loss = premium paid; we mostly hold to resolution). `realized_pct` + `peak/trough_unrealized_pct` are the natural normalizers.
- **External ordered feature-vector + manifest:** for a fixed-input NN. Our consumer is a future Claude reading **named JSON**; a versioned named blob is simpler and sufficient.
- **MiFID-grade clock traceability / per-field microsecond capture-vs-event split:** regulatory overkill at 5m/15m frequency.
- **Precomputed `sample_uniqueness_weight`:** bakes in a model assumption. Log the input (`overlap_group_id`); let the future trainer compute weights.
- **Multi-logger tagging:** we run one strategy per mode; `policy_id` + `schema_version` already disambiguate eras.

---

## 4. Architecture

```
                          ┌─────────────────────────────────────────────┐
   ENTRY (decision tick)  │  strategy_runner.py / trigger_engine.py      │
                          │  build decision_snapshot (NEW):              │
                          │   - call/reuse compute_signals() result      │
                          │   - policy params, regime, feed provenance   │
                          └───────────────┬─────────────────────────────┘
                                          │ context= (existing param, extended)
                                          ▼
                          ┌─────────────────────────────────────────────┐
   demo_engine.py         │  simulate_market_buy / record_live_buy       │
   (thin hooks)           │   → audit_tracker.open_row(session_id, snap) │  ← decision-time, immutable
                          │                                              │
                          │  simulate_sell_all / expire_all_outside_*    │
                          │   → audit_tracker.finalize_row(session_id,…) │  ← settlement-time append + derived/cf
                          └───────────────┬─────────────────────────────┘
                                          ▼
                          ┌─────────────────────────────────────────────┐
   audit_tracker.py (NEW) │  audit.db (SQLite on /data)                  │
                          │   promoted indexed columns + context_json    │
                          │   shared derive_learning_fields() (offline-  │
                          │   safe, reads only the immutable snapshot)   │
                          └───────────────┬─────────────────────────────┘
                                          ▼
   main.py (NEW endpoints) GET /api/audit · GET /api/audit/{session_id} · GET /api/audit/export
                                          ▼
   src/AuditTab.tsx (NEW)  📋 ביקורת עסקאות  — table + drill-down + filters + summary
```

### New / changed components
- **`engine/audit_tracker.py` (NEW)** — twin of `fault_tracker.py`. Owns `audit.db`, exposes `open_row`, `finalize_row`, `list_audits`, `get_audit`, `audit_counts`, `export_rows`, plus a shared `derive_learning_fields(snapshot, outcome)` used by both the writer and (later) any offline recompute.
- **`engine/strategy_runner.py` (CHANGE)** — build a `decision_snapshot` at the entry tick (wire `compute_signals` result + policy params + regime + provenance) and pass it through the existing `context=` channel. This is the core engine change.
- **`engine/demo_engine.py` (CHANGE)** — thin hooks: on BUY → `open_row`; on SELL_TP / settlement → `finalize_row`. No new bloat in `demo_state.json`.
- **`engine/main.py` (CHANGE)** — 3 read endpoints (list / detail / export), following the faults endpoint shape. Analytics reads (cacheable; **not** order-path).
- **`src/AuditTab.tsx` (NEW)** + tab registration in `src/App.tsx` — follows `FaultsTab.tsx` (12s poll, skip when hidden).

### Why a separate `audit.db` (chosen approach)
Keeps the rich per-trade record **out of the perf-sensitive `demo_state.json`** (honoring recent perf work), gives the future AI a clean SQL surface, and reuses the established `faults.db`/`history.db` pattern. Storage is **hybrid**: ~12 high-value fields promoted to real indexed columns for query/mining; the complete decision snapshot kept in a versioned `context_json` blob for completeness + immutability.

---

## 5. Data Model — `audit.db`

One row per **trade-session** (entry→exit/settlement lifecycle, keyed by `session_id`). Columns grouped by **knowledge-time**. `context_json` holds the full snapshot; promoted columns are a point-in-time-correct projection for indexing.

### Table `audit_rows`

**Identity & versioning**
| column | type | notes |
|---|---|---|
| `session_id` | TEXT PK | groups BUY(s)→SELL_TP/SETTLE |
| `schema_version` | INTEGER | monotonic; bump on any meaning change |
| `code_version` | TEXT | git SHA at decision time |
| `mode` | TEXT | `demo` \| `live` |
| `slug` | TEXT | market slug |
| `epoch` | INTEGER | window epoch |
| `window_sec` | INTEGER | 300 \| 900 |
| `side` | TEXT | `Up` \| `Down` |

**Decision-time (immutable after entry tick)**
| column | type | notes |
|---|---|---|
| `decision_ts` | INTEGER | UTC epoch-ms, written once |
| `seconds_remaining_at_entry` | INTEGER | |
| `entry_minute_in_window` | INTEGER | |
| `recommendation` | TEXT | promoted from signal snapshot |
| `weighted_score` | REAL | promoted |
| `confidence_pct` | REAL | promoted |
| `vol_bucket` | TEXT | `low`\|`mid`\|`high`, stamped at decision |
| `btc_spot_at_entry` | REAL | |
| `avg_fill_price` | REAL | |
| `contracts` | REAL | |
| `investment_usd_effective` | REAL | |
| `loss_recovery_multiplier` | REAL | promoted (incident-relevant) |
| `action_propensity` | REAL | default 1.0 (schema-ready for off-policy) |
| `exploration_flag` | INTEGER | default 0 |
| `context_json` | TEXT | **full** decision snapshot (see §6) |

**Settlement-time (append only; never overwrite decision fields)**
| column | type | notes |
|---|---|---|
| `settled_ts` | INTEGER | UTC epoch-ms; invariant `> decision_ts` |
| `exit_type` | TEXT | mechanism: `TP`\|`settle`\|`voided`\|`attempt` (attempt = unfilled entry, see §7) |
| `settlement_status` | TEXT | **enum** `WIN`\|`LOSS`\|`VOID`\|`INVALID`\|`UNKNOWN`\|`PENDING` (default `PENDING`) |
| `realized_pnl` | REAL | |
| `realized_pct` | REAL | promoted |
| `peak_unrealized_pct` | REAL | from existing watermark |
| `trough_unrealized_pct` | REAL | from existing watermark |
| `hold_duration_sec` | REAL | |
| `fees` | REAL | |
| `settlement_btc_start` | REAL | |
| `settlement_btc_end` | REAL | |
| `resolved_outcome` | TEXT | `Up`\|`Down`\|`null` |

**Derived & counterfactual (computed at finalize from the immutable snapshot + outcome)**
| column | type | notes |
|---|---|---|
| `exit_efficiency` | REAL | `realized_pct / peak_unrealized_pct`; `null` when `peak ≤ 0` (no favorable excursion) |
| `missed_profit_pct` | REAL | `peak_unrealized_pct − realized_pct` |
| `signal_was_correct` | INTEGER | chosen side == resolved outcome |
| `signals_agreement` | REAL | consensus score across TA/CLOB/sentiment |
| `signal_conflict` | INTEGER | entered against majority signal |
| `cf_other_side_pnl` | REAL | promoted; opposite leg's resolved payoff |
| `dipped_then_won` | INTEGER | trough<0 then WIN |
| `lesson_tag` | TEXT | promoted; e.g. `clean_win`, `good_entry_late_exit`, `signal_conflict_loss` |
| `rule_flags_json` | TEXT | `{entered_late, above_price_cap, against_signal, recovery_active,…}` |
| `cf_exit_variants_json` | TEXT | `{pnl_if_held_to_resolution, pnl_at_peak, pnl_at_trough}` |
| `overlap_group_id` | TEXT | epoch/window-derived (for future purged CV) |

**Indexes:** `decision_ts`, `(mode, window_sec)`, `settlement_status`, `side`, `lesson_tag`, `recommendation`.

### `context_json` is the source of truth for completeness
Promoted columns are for SQL filtering; `context_json` carries **everything** (all raw TA/CLOB/sentiment/history sub-dicts, full policy params, full provenance) keyed by `schema_version`, so nothing is ever lost for the future AI even if it was not promoted to a column.

---

## 6. The Decision Snapshot (`context_json`, schema_version 1)

Built at the entry tick in `strategy_runner` and passed via the existing `context=` channel. Named JSON (not a positional vector). Shape:

```jsonc
{
  "schema_version": 1,
  "code_version": "<git-sha>",
  "decision_ts": 1733385600123,           // UTC ms
  "signal": {                              // from compute_signals() — NEWLY wired in
    "recommendation": "Up", "up_confidence": 0.63, "down_confidence": 0.37,
    "weighted_score": 0.26, "confidence_pct": 63.0,
    "weights": {"ta":0.40,"clob":0.30,"history":0.15,"sentiment":0.15}
  },
  "ta": {"rsi":58.2,"ema9":...,"ema21":...,"ema_diff":...,
         "momentum_3m_pct":0.04,"momentum_5m_pct":0.09,"ta_score":2},
  "clob": {"up_imbalance":0.12,"down_imbalance":-0.08,"net_score":0.20,"spread":0.02},
  "sentiment": {"funding_rate_pct":-0.01,"fear_greed_value":41,"sentiment_score":1},
  "history": {"hour_up_rate":0.57,"hour_sample_size":120,"overall_up_rate":0.51},
  "flw": {"enabled":true,"mode":"forward","prior_winner":"Up","decision":"Up"},
  "trigger": {"mode":"signal","btc_change_pct":0.05,"contract_ask":0.52,"contract_drift_pct":0.03},
  "regime": {"vol_bucket":"mid","btc_change_pct_at_entry":0.05,
             "seconds_remaining_at_entry":210,"entry_minute_in_window":1},
  "policy": {                              // policy snapshot — incident-relevant
    "policy_id":"strategy_v?", "order_mode":"market", "take_profit_pct":50,
    "entry_price_cents_cap":65, "side_preference":"signal",
    "min_minutes_for_entry":0, "investment_usd_base":..., "investment_usd_effective":...,
    "loss_recovery_enabled":true, "loss_recovery_multiplier":2.0, "loss_recovery_streak":1
  },
  "provenance": {                          // WS-first/REST-fallback freshness
    "btc_spot_source":"ws","btc_spot_age_ms":120,
    "book_source":"ws","book_age_ms":80,
    "signals_stale":false,"signals_missing":false
  },
  "execution": {"avg_fill_price":0.52,"contracts":40,"arrival_mid":0.515,
                "entry_slippage":0.005,"gate":"signal","reason":"..."}
}
```

**Point-in-time guarantee:** every field above is the value as known at `decision_ts`. `finalize_row` reads this immutable blob; it never recomputes a signal from live data.

---

## 7. Lifecycle, Wiring & Guardrails

1. **Entry (the core change).** In `strategy_runner` (and the trigger paths), assemble `decision_snapshot`. Prefer reusing the already-computed `compute_signals` result (cache exists in `main.py:209`) to avoid extra feed calls; if absent in a given path, mark `signals_missing=true` rather than fabricating values. Pass the snapshot through the existing `context=` param into `simulate_market_buy`/`record_live_buy`. `demo_engine` calls `audit_tracker.open_row(session_id, snapshot)` (idempotent; first BUY of a session wins, DCA slices update sizing only).
2. **Hold.** No change — `pnl_path` + peak/trough watermarks already tracked in `_position_tracking`.
3. **Close / settle.** On SELL_TP (`simulate_sell_all`/`record_live_sell`) and on settlement (`expire_all_outside_tokens`), `demo_engine` calls `audit_tracker.finalize_row(session_id, outcome)`. `finalize_row`:
   - writes settlement-time columns + `settled_ts` (assert `> decision_ts`),
   - sets `settlement_status` from the existing `resolved_outcome`/`settlement_won`/`voided` logic (VOID/UNKNOWN map explicitly — never to a number),
   - computes derived + counterfactual fields via shared `derive_learning_fields()`.
4. **Martingale guardrail tie-in.** `loss_recovery` must consult `settlement_status` and update streak/multiplier **only** on `{WIN, LOSS}` — quarantining `VOID/INVALID/UNKNOWN/PENDING`. This is the same enum the export uses to drop non-label rows. (Closes the −85% incident loop at the source.) *Note: if the engine already guards SETTLE_UNKNOWN post-incident, this spec formalizes the shared enum; verify and reconcile during implementation.*
5. **Attempt logging (anti-survivorship, thin add).** Rejected/no-fill/expired entry attempts get a lightweight row with `exit_type='attempt'`, `settlement_status='INVALID'`, and `rule_flags_json.outcome_reason`. Phase-A priority is filled+settled rows; attempt rows are additive and never pollute the label set (excluded by the `{WIN,LOSS}` filter).

---

## 8. Backfill

Historical closed sessions in `demo_state.trades` already carry **outcome + peak/trough + settlement BTC + pnl_path** → backfill audit rows with the **settlement-time + derived + counterfactual** layers and `schema_version = 0` (pre-signal-capture era). **Entry decision signals were never captured historically, so Layer B (`signal`/`ta`/`clob`/`sentiment`) will be null/partial in backfilled rows.** New trades (schema_version ≥ 1) get the full picture. Stating this honestly: the ledger does **not** retroactively know *why* old trades were entered; it knows *what happened* to them. `schema_version` makes the two eras cleanly separable so the future AI never conflates them.

---

## 9. API

Following the `/api/faults` shape (`engine/main.py`), all read-only analytics endpoints (cacheable; **not** order-path, so the no-cache order guardrails do not apply):

- `GET /api/audit?mode=&window_sec=&settlement_status=&side=&lesson_tag=&limit=1000` → `{ rows: AuditRow[], counts: {...} }`
- `GET /api/audit/{session_id}` → full row incl. parsed `context_json` + `cf_exit_variants` + `pnl_path` (joined for the drill-down chart).
- `GET /api/audit/export?since_ts=&schema_version=&format=jsonl` → compact, full-fidelity dump (named JSON per row) — **the future AI's entry point.** Documented as the Phase-B/C contract.

`counts` summary: total, by `settlement_status`, win-rate over `{WIN,LOSS}` only, avg `exit_efficiency`, top `lesson_tag` frequencies.

---

## 10. UI — `📋 ביקורת עסקאות` tab

Follows `src/FaultsTab.tsx` (12s poll via `api<T>()`, skip when `isPageHidden()`), registered in `src/App.tsx` (`Tab` type, tab-button tuple array, conditional render, import).

- **Summary header:** # sessions, win-rate (over `{WIN,LOSS}`), avg exit-efficiency, top lessons by frequency, counts by `settlement_status`.
- **Table** (one row per closed session, newest first): time · market (5m/15m) · side · entry price · contracts · `exit_type` · realized PnL · ROI% · peak% · exit-efficiency · signal-vs-outcome (✓/✗) · signals-agreement · `lesson_tag`.
- **Row drill-down panel:** the full story — entry signal snapshot (TA/CLOB/sentiment/history), policy params, provenance/freshness, regime; the existing `pnl_path` mini-chart; settlement details; counterfactuals (`cf_other_side_pnl`, exit variants); derived verdict + `rule_flags`; and the raw `context_json`.
- **Filters:** mode (demo/live) · window (5m/15m) · `settlement_status` · side · win/loss · `lesson_tag`.

Visual layout follows the existing dashboard pattern (no new design language).

---

## 11. Testing Strategy

- **`audit_tracker` unit tests** (twin of `test_fault_tracker.py`): open→finalize lifecycle; `settled_ts > decision_ts` invariant; decision-field immutability (finalize must not mutate decision columns); `settlement_status` mapping for WIN/LOSS/VOID/UNKNOWN; `context_json` round-trip.
- **`derive_learning_fields` tests:** exit_efficiency, missed_profit, signal_was_correct, `cf_other_side_pnl` for a binary win/loss, cf_exit_variants from a synthetic `pnl_path`, lesson_tag classification.
- **Guardrail test:** loss_recovery streak/multiplier does **not** advance on `VOID`/`UNKNOWN` (regression test for the incident).
- **Wiring test:** a simulated entry produces a non-null Layer B snapshot (catches the "compute_signals not wired" regression).
- **Backfill test:** historical trade → audit row with `schema_version=0`, null Layer B, correct outcome/derived fields.
- **API smoke** (extend `test_api_smoke.py`): list/detail/export shapes; export excludes non-`{WIN,LOSS}` from labels.

---

## 12. Risks & Open Questions

- **Engine-change blast radius.** Wiring `decision_snapshot` into `strategy_runner` touches the hot entry path. Mitigation: snapshot assembly must be cheap (reuse cached `compute_signals`), wrapped so a snapshot failure **never** blocks a trade (audit is best-effort; log to `fault_tracker` on failure).
- **Double source of truth for settlement status.** If the engine already special-cases `SETTLE_UNKNOWN` post-incident, reconcile to the shared enum rather than adding a parallel notion. *(Verify in implementation.)*
- **`audit.db` growth.** Bounded like `faults.db`; add retention/rotation if needed. `pnl_path` is referenced for the drill-down — decide whether to store a trimmed copy on the row or join from existing storage (lean toward join/trim to avoid duplication).
- **Open question for the user:** should backfill run automatically on first deploy, or be a manual one-off script? (Default: manual script, to keep deploy side-effect-free.)

---

## 13. Sources (research grounding)

Trade journaling / TCA: Edgewonk (Tradeciety), ForexMechanics, FundingRock, Talos TCA, Perold Implementation Shortfall. Point-in-time / feature stores: Google Rules of ML (#29/#31/#32), apxml point-in-time correctness, Feast/Tecton, AWS ML Lens MLREL-07, quantreo look-ahead bias. Label/reward & offline-RL: D4RL (Fu et al. 2020), Strehl et al. 2010 (logged implicit exploration), Counterfactual Risk Minimization (Swaminathan & Joachims), López de Prado triple-barrier & meta-labeling (Sefidian/Hudson&Thames), purged cross-validation. Data pitfalls: lookahead/leakage (MQL5, Kyle Jones), survivorship (QuantRocket), regime non-stationarity, UMA/Polymarket void/invalid resolution semantics.

---

## 14. תקציר בעברית

נבנה **ספר ביקורת ולמידה לעסקאות** — רשומה אחת לכל עסקה (כניסה→יציאה/הסדרה) במסד נפרד `audit.db`, שתופסת את **כל** הסיפור: *למה נכנסנו* (כל הסיגנלים ברגע ההחלטה — היום מחושבים ונזרקים!), *מה קרה*, ו*מה היה אפשר טוב יותר* (יעילות יציאה, רווח שהוחמץ, והתוצאה ההפוכה/יציאות חלופיות — כמעט בחינם בשוק בינארי). השינוי המרכזי במנוע: **לחבר את `compute_signals` להקשר הכניסה** ב-`strategy_runner` (כרגע לא מחובר). עקרונות מפתח מהמחקר: לתפוס נתונים *ברגע ההחלטה* בלי דליפת עתיד; שדות-החלטה לא משתנים אחרי הכניסה; `settlement_status` כ-enum נפרד מ-PnL שגם המרטינגייל וגם ה-AI מתעלמים ממנו אם הוא לא `WIN/LOSS` (סוגר את תקרית ה-85%−); גרסת schema לכל שורה. לא נבנה את הלולאה האוטונומית עכשיו — נבנה את **הדלק** שה-AI יצטרך, עם נקודת קצה `/api/audit/export` כשער הכניסה שלו, וטאב `📋 ביקורת עסקאות` שתלמד ממנו כבר היום.
