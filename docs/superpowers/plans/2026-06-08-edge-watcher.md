# Edge-Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A recording-only/advisory module that scans `audit.db` and emits one plain-Hebrew verdict (`collecting`→`watching`→`forming`→`confirmed`) telling the owner when a statistically genuine, tradeable edge has emerged — tuned hard toward false-negatives.

**Architecture:** Pure-Python stats leaves (`edge_stats.py`) → analysis orchestrator (`edge_watcher.py`, mirrors `trade_coach.py`, never raises, off-loop) → cached endpoint `GET /api/audit/edge` → new `🔭 גלאי edge` React tab. A private SQLite sidecar tracks forward-time persistence. **No imports from trading code; no writes outside its own tables; the autonomy switch stays human-only.**

**Tech Stack:** Python 3 stdlib only (`math`, `statistics`, `sqlite3`, `json` — no scipy/numpy), FastAPI (existing `main.py`), React/TS (existing dashboard). Full algorithm rationale: `docs/superpowers/specs/2026-06-08-edge-watcher-design.md`.

**Invariant every task must preserve:** the module is reachable ONLY via the cached endpoint on a worker thread; every public fn never raises (returns a safe empty/`collecting` result); nothing it does can alter a trade, config, or `audit_rows`.

---

## File Structure

- Create `engine/edge_stats.py` — pure statistical leaves, no I/O, no deps beyond stdlib.
- Create `engine/edge_watcher.py` — `detect_edges(rows, *, config)` orchestrator + extractors + scans.
- Create `engine/edge_persistence.py` — private SQLite sidecar (`hypotheses`, `edge_verdicts`); forward-confirmation counter. Separate file so the pure analysis stays I/O-free and unit-testable.
- Modify `engine/main.py` — add `GET /api/audit/edge` + `_AUDIT_EDGE_CACHE`.
- Create `src/EdgeWatcherTab.tsx` — the tab UI.
- Modify `src/App.tsx` — `Tab` union, tab array, render line.
- Create tests under `engine/tests/`: `test_edge_stats.py`, `test_edge_watcher.py`, `test_edge_persistence.py`.

Constants (single source of truth at the top of `edge_watcher.py`):
```python
TP_PCT = 18.0
REAL_RATE = 0.035          # real Polymarket round-trip wedge
DEMO_FEE_RATE = 0.002      # already booked in the ledger (per side)
STAKE_USD = 5.0
TOTAL_MIN = 800            # below -> "collecting"
N_SLICE_MIN_EFFECTIVE = 400  # effective (design-effect-adjusted) slice size
FIRE_RATE_MIN = 0.05
MIN_RAW_LIFT_PTS = 5.0
ECON_MIN_NET = 0.10        # +$ per unit stake (master gate)
ECON_ABSTAIN_NET = -0.10   # E: genuinely costly
BH_Q = 0.10
DSR_MIN = 0.95
MIN_CONFIRMATIONS = 3
CONFIRM_SPACING_TRADES = 100  # >= this many new settled trades between confirmations
```

---

### Task 1: `edge_stats.py` — pure statistical leaves

**Files:**
- Create: `engine/edge_stats.py`
- Test: `engine/tests/test_edge_stats.py`

Functions (all pure, deterministic, no exceptions on degenerate input — return safe values):
`norm_cdf(z)`, `norm_ppf(p)` (Acklam/rational approx, no scipy); `wilson_bounds(k, n, z=1.96) -> (lo, hi)`; `two_proportion_p(k1,n1,k2,n2, alternative) -> float` (one-sided two-proportion z-test, slice-vs-complement); `bh_fdr(pvals, q) -> list[bool]` (Benjamini-Hochberg reject mask); `deflated_sharpe(returns, n_trials, gamma=0.5772) -> float`; `day_block_bootstrap(values, day_keys, stat_fn, iters, seed) -> list[float]` (resample by day-block, deterministic via seed param); `day_block_perm_pvalue(in_slice, day_keys, labels, iters, seed) -> float` (permutation p-value respecting day blocks); `tertiles(values) -> (q33, q66)`.

- [ ] **Step 1: Write failing tests** for each leaf against known values.

```python
# engine/tests/test_edge_stats.py
import math
from engine import edge_stats as es

def test_norm_cdf_known():
    assert abs(es.norm_cdf(0.0) - 0.5) < 1e-9
    assert abs(es.norm_cdf(1.96) - 0.975) < 1e-3

def test_norm_ppf_inverse():
    assert abs(es.norm_ppf(0.975) - 1.96) < 1e-3

def test_wilson_bounds_basic():
    lo, hi = es.wilson_bounds(50, 100)
    assert lo < 0.5 < hi and 0.39 < lo < 0.41 and 0.59 < hi < 0.61

def test_wilson_degenerate():
    assert es.wilson_bounds(0, 0) == (0.0, 0.0)   # never raises

def test_two_proportion_one_sided():
    # slice 64/100 vs complement 47/100, slice>complement
    p = es.two_proportion_p(64, 100, 47, 100, alternative="greater")
    assert 0.0 < p < 0.02

def test_bh_fdr_rejects_expected():
    pvals = [0.001, 0.009, 0.02, 0.5, 0.8]
    mask = es.bh_fdr(pvals, q=0.10)
    assert mask[0] is True and mask[-1] is False

def test_bh_fdr_all_null():
    assert es.bh_fdr([0.6, 0.7, 0.9], q=0.10) == [False, False, False]

def test_bh_fdr_empty():
    assert es.bh_fdr([], q=0.10) == []

def test_day_block_bootstrap_deterministic():
    vals = [1.0, -0.5, 0.3, 0.2, -0.1, 0.4]
    days = ["a","a","b","b","c","c"]
    out1 = es.day_block_bootstrap(vals, days, lambda xs: sum(xs)/len(xs), iters=500, seed=7)
    out2 = es.day_block_bootstrap(vals, days, lambda xs: sum(xs)/len(xs), iters=500, seed=7)
    assert out1 == out2 and len(out1) == 500

def test_tertiles_monotone():
    q33, q66 = es.tertiles(list(range(100)))
    assert q33 < q66
```

- [ ] **Step 2:** Run `python -m pytest engine/tests/test_edge_stats.py -q` → expect FAIL (module missing).
- [ ] **Step 3:** Implement `engine/edge_stats.py`. Use `random.Random(seed)` (NOT global random) for determinism. Every fn guards degenerate input (`n==0`, empty lists, all-same-day) and returns safe values (never raises). `norm_ppf` via Acklam rational approximation.
- [ ] **Step 4:** Run the test file → expect PASS.
- [ ] **Step 5:** Commit `feat(edge): edge_stats.py pure statistical leaves + tests`.

---

### Task 2: Row extractors — `y_tp`, `y_dir`, `r_net` (stake-normalized, real-fee), `clean`

**Files:**
- Create (start): `engine/edge_watcher.py` (constants + extractors only this task)
- Test: `engine/tests/test_edge_watcher.py`

Extractors operate on one row dict (the `light=False` shape from `audit_tracker.export_rows`). They must tolerate missing/None/malformed fields without raising.

```python
def y_tp(row) -> int:            # 1 iff realized TP exit (single definition — spec I1)
    return 1 if row.get("exit_type") == "TP" else 0

def y_dir(row):                  # 1/0/None — directional held-to-resolution, demo-fee-netted
    cf = row.get("cf_exit_variants") or {}
    v = cf.get("pnl_if_held_to_resolution")
    if v is None or row.get("resolved_outcome") not in ("Up", "Down"):
        return None
    return 1 if v > 0 else 0

def r_net(row) -> float | None:  # stake-normalized, real-fee net $ (spec 3.1, fixes I3/I6)
    rp = row.get("realized_pnl")
    if rp is None: return None
    fill = _num(row.get("fill_price")) ; contracts = _num(row.get("contracts"))
    wedge = (REAL_RATE - 2*DEMO_FEE_RATE) * fill * contracts if (fill and contracts) else (REAL_RATE - 2*DEMO_FEE_RATE) * STAKE_USD
    mult = max(_num(row.get("loss_recovery_multiplier")) or 1.0, 1.0)
    return (rp - wedge) / mult

def clean(row) -> bool:          # martingale/exploration confound filter (spec G5)
    rf = row.get("rule_flags") or {}
    return (rf.get("recovery_active") is not True
            and (_num(row.get("loss_recovery_multiplier")) or 1.0) <= 1.0
            and row.get("exploration_flag") in (0, False, None))
```

- [ ] **Step 1: Write failing tests** — TP/non-TP rows; directional None on VOID/non-resolved; `r_net` uses real notional when fill/contracts present and falls back to stake; stake-normalization divides by multiplier; `clean` drops recovery/exploration rows; **malformed rows (missing keys, None numerics, strings) return safe values, never raise.**
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement constants + extractors + `_num` helper in `edge_watcher.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(edge): row extractors (y_tp/y_dir/r_net/clean) + tests`.

---

### Task 3: Bucketizer + walk-forward split (frozen edges per fold train-segment)

**Files:**
- Modify: `engine/edge_watcher.py`
- Test: `engine/tests/test_edge_watcher.py`

```python
FEATURES = [  # (path_fn, name, kind)  kind in {"cont","cat"}
  # continuous TA: context["ta"]["features"][k]; CLOB: context["clob"][side][k]; categorical as-is
]
def feature_value(row, feat): ...            # safe nested read -> float|str|None
def _walk_forward_split(rows):               # sort by decision_ts asc; seal most-recent 30% vault
    # returns (discovery_70, oos_vault_30); NEVER shuffle
def _folds(discovery, k=5):                  # >=5 expanding-window folds, embargo 2 boundary trades
def bucketize(train_rows, feat):             # tertile edges from TRAIN SEGMENT ONLY (spec I5)
    # returns a frozen mapping value->bucket label; reused unchanged on test/vault
```

- [ ] **Step 1: Write failing tests** — split keeps 70/30 by time, never shuffles; vault is the most-recent rows; folds expand and embargo 2 boundary trades; **leak guard (T8): tertile edges computed on a fold's train segment do NOT change when test data is appended** (construct a case where refitting on the full set would move a boundary and assert the frozen edge ignores it).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. `FEATURES` lists the ~40 (feature, bucket, kind) entries from spec §3.1; continuous → tertiles, categorical → as-is.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(edge): walk-forward split + frozen-edge bucketizer + leak-guard tests`.

---

### Task 4: Slice evaluator — gates G0–G5 with day-block p-value + slice-vs-complement

**Files:**
- Modify: `engine/edge_watcher.py`
- Test: `engine/tests/test_edge_watcher.py`

```python
def _evaluate_slice(disc_rows, vault_rows, mask_fn, target) -> dict:
    """Returns {n_eff, fire_rate, lift_pct, hit_rate_pct, baseline_pct,
               wilson_ok, pvalue (day-block), r_net_mean, r_net_p5_boot,
               n_losers, dsr, stability{...}, clean_survives, passes_g0..g5}"""
```
Wire, per spec §3.2–3.7: **slice-vs-complement** two-proportion (baseline = discovery complement), **day-block permutation p-value** (NOT binomial), effective-n gate (`N_SLICE_MIN_EFFECTIVE`), economic master gate with tail (`r_net_mean>=ECON_MIN_NET` AND day-block 5th-pct>0 AND `n_losers>=` floor), regime stability (≥3/4 folds, top-day<40%, ≥2/3 vol regimes), and `clean()` survival.

- [ ] **Step 1: Write failing tests** — a planted clean slice passes G0–G5; a slice tested vs complement (not constant); the day-block p-value is used (assert binomial is NOT called — e.g. a slice that is binomially-significant but day-block-insignificant must FAIL G2); economic gate rejects a high-TP-rate slice whose `r_net_mean<ECON_MIN_NET` (T5); martingale-only slice fails G5 (T6).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(edge): slice evaluator with day-block p-value + slice-vs-complement gates + tests`.

---

### Task 5: `edge_persistence.py` — private SQLite sidecar + forward-confirmation counter

**Files:**
- Create: `engine/edge_persistence.py`
- Test: `engine/tests/test_edge_persistence.py`

Private connection to a sidecar DB under `DATA_ROOT` (e.g. `edge_state.db`) — NEVER opens `audit.db` for write. Tables `hypotheses(scan_ts, m_count)` and `edge_verdicts(slice_key, first_seen_ts, last_max_decision_ts, consecutive_confirmations, last_state)`. API: `record_scan(m_count)`, `bump_confirmation(slice_key, max_decision_ts) -> int` (increments only when ≥`CONFIRM_SPACING_TRADES` new settled trades since `last_max_decision_ts`, else holds; resets to 0 if the slice failed gates this scan), `confirmations(slice_key) -> int`. Never raises (try/except → safe defaults).

- [ ] **Step 1: Write failing tests** (tmp DB path) — first sighting → confirmations 1; another scan within spacing → still 1; a scan after ≥spacing new trades → 2; a failed-gate scan resets to 0; concurrent open never raises; **assert it never opens/writes audit.db.**
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(edge): persistence sidecar + forward-confirmation counter + tests`.

---

### Task 6: `detect_edges` orchestrator + state machine + persistence (G6)

**Files:**
- Modify: `engine/edge_watcher.py`
- Test: `engine/tests/test_edge_watcher.py`

```python
def detect_edges(rows, *, config=None) -> dict:
    """Never raises. Filters to settled labeled rows; if < TOTAL_MIN -> 'collecting'.
    Runs B (tp_reach) + E (abstention) scans + A (directional diagnostic). BH-FDR over
    honest per-scan m. Applies G6 persistence via edge_persistence. Builds EdgeResponse."""
```
State machine per spec §3.8. Directional only sets `directional_note_he` (labeled "(לפני עמלות אמיתיות)"). `confirmed` requires ≥`MIN_CONFIRMATIONS` forward confirmations. Ambiguity → lower state.

- [ ] **Step 1: Write failing tests — the critical safety battery:**
  - **T1** planted clean edge across ≥3 days/4 folds/3 vol-buckets, with simulated forward confirmations → eventually `confirmed`, `tp_reach` card, `confidence high`.
  - **T2 (false-positive)** 1,500 i.i.d. Bernoulli(0.53) rows independent of ~40 random features, **AND** a second variant where the noise is **autocorrelated by day-block** (same-day rows share a regime) — across a 30-seed sweep assert `state in {"collecting","watching"}` and `best_candidate is None`. This is the gating test; the autocorrelated variant is the one that would break a naive binomial design.
  - **T5** economic gate dominates significance. **T6** martingale-artifact never `confirmed`. **T9** strong directional slice only sets the note, never a card. **T11** a single passing scan yields at most `forming` (needs ≥3 forward confirmations) — only after enough spaced confirmations does it reach `confirmed`. **T7** empty/all-PENDING/malformed rows → safe `collecting`, no exception.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement orchestrator + state machine + `EdgeResponse` builder (shape per spec §4, incl `trades_min_needed_in_slice`). **Step 4:** Run full `engine/tests/test_edge_watcher.py` → PASS.
- [ ] **Step 5:** Commit `feat(edge): detect_edges orchestrator + state machine + safety battery (T1/T2/T5/T6/T9/T11)`.

---

### Task 7: Endpoint `GET /api/audit/edge` (off-loop, cached)

**Files:**
- Modify: `engine/main.py`
- Test: `engine/tests/test_edge_watcher.py` (endpoint smoke via TestClient or direct fn)

Mirror `/api/audit/lessons` exactly: new `_AUDIT_EDGE_CACHE = TTLCache(ttl_sec=60)`; handler reads `audit_tracker.export_rows(labels_only=True, light=False, limit=100000)` and runs `edge_watcher.detect_edges` inside `asyncio.to_thread`, caches, returns the `EdgeResponse`. Pass `config={"take_profit_pct": getattr(runner.rt.config,"take_profit_pct",18.0)}`.

- [ ] **Step 1: Write failing test** — call the endpoint (or its `_work`) against a synthetic ledger; assert valid `EdgeResponse` keys, off-loop (uses to_thread), cached (2nd call within TTL doesn't re-scan — assert via a call counter/monkeypatch).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(edge): GET /api/audit/edge endpoint (cached, off-loop)`.

---

### Task 8: `EdgeWatcherTab.tsx` + tab wiring + autonomy-nudge guard

**Files:**
- Create: `src/EdgeWatcherTab.tsx`
- Modify: `src/App.tsx` (`Tab` union, tab array before `"audits"`, render line)

Render the 3 states from spec §5 (Hebrew RTL), styled like `AuditTab.tsx`. `VerdictHero` + `ProgressMeter` (collecting/forming) + `CandidateGrid` of `EdgeCard` + `HonestyFooter`. 12s refresh skipping `isPageHidden()`. The **`mayNudgeAutonomy`** guard (spec §5) is the ONLY place a button shows; the button calls `setTab("strategy")` + scroll-anchors to the `🤖 מצב החלטה` block — it **must NOT** call `setDecisionMode`. Hero verb always "שקול".

- [ ] **Step 1:** Build the component + wiring. (UI: verify by build + render, not unit test.)
- [ ] **Step 2:** Run `npm run build` (tsc+vite) → expect clean.
- [ ] **Step 3:** Grep-assert no `setDecisionMode`/mode-write call exists in `EdgeWatcherTab.tsx`.
- [ ] **Step 4:** Commit `feat(edge): EdgeWatcherTab UI + tab wiring + human-only autonomy guard`.

---

### Task 9: Full-suite + end-to-end smoke

**Files:** none new.

- [ ] **Step 1:** Run the entire python suite (`python -m pytest engine/tests -q`) → all pass (no regression to the existing 391).
- [ ] **Step 2:** `npm run build` clean.
- [ ] **Step 3:** Boot the engine locally against a copy of a real `audit.db`; `curl /api/audit/edge` → returns `collecting`/`watching` on thin data, off-loop, no exception, and the hot loop is unaffected (verify via the verify skill / runtime surface).
- [ ] **Step 4:** Commit any smoke fixes. Leave deploy for explicit user go (the spec is recording-only but still a money-adjacent service; restore mode=auto only after the user approves the deploy).
