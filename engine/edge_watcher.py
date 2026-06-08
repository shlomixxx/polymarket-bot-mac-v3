"""Edge-Watcher analysis orchestrator (recording-only / advisory).

Mirrors trade_coach.py: pure functions over a list of audit-row dicts (the
`light=False` shape from audit_tracker.export_rows). It scans the ledger and emits
ONE plain-Hebrew verdict (collecting -> watching -> forming -> confirmed) telling the
owner when a statistically genuine, tradeable edge has emerged.

INVARIANTS (load-bearing — see docs/superpowers/specs/2026-06-08-edge-watcher-design.md):
  * RECORDING-ONLY: imports NOTHING from trading code (no demo_engine / strategy_runner /
    runner / order path). It never writes audit_rows. The only writes (in later tasks) are
    to the private edge_state sidecar DB.
  * NEVER RAISES: every public fn returns a safe value on malformed / empty input, never an
    exception (mirror trade_coach.compute_lessons' defensive style).
  * OFF THE EVENT LOOP: reached only via the cached endpoint on a worker thread.
  * The ADVERSARIAL STAT-FIXES are load-bearing — implement the FIXED versions from the spec.

This task (Task 2) ships only: the constants block + the four row extractors
(y_tp / y_dir / r_net / clean) + the `_num` numeric helper. Later tasks add the
bucketizer, slice evaluator, persistence wiring and the detect_edges orchestrator.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Optional

import edge_stats as es


# ── Constants: single source of truth (spec §3.1, §7) ───────────────────────
TP_PCT = 18.0
REAL_RATE = 0.072          # real Polymarket crypto round-trip wedge: the Jan-2026
                           # dynamic taker fee (feeRate*p*(1-p) per share, crypto
                           # feeRate≈0.07 → ≈3.6% of notional per side at 50/50)
                           # ≈ 7.2% round-trip (+spread ≈ ~8% all-in)
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


# ── Defensive numeric coercion ──────────────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Never raises.

    Tolerates None, numeric strings, ints/floats. Rejects NaN/Inf, lists, dicts,
    booleans-as-numbers are accepted (bool is an int) but callers don't rely on that.
    """
    if v is None or isinstance(v, (list, dict, tuple, set)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


# ── Row extractors (operate on ONE row dict; tolerate malformed rows) ───────
def y_tp(row: Any) -> int:
    """1 iff realized TP exit (single definition — spec I1).

    Does NOT blend in the peak>=18 near-miss counterfactual (that is a secondary
    diagnostic only). Any non-dict / missing field returns 0.
    """
    try:
        return 1 if (isinstance(row, dict) and row.get("exit_type") == "TP") else 0
    except Exception:
        return 0


def y_dir(row: Any) -> Optional[int]:
    """1 / 0 / None — directional held-to-resolution, demo-fee-netted.

    None unless the row resolved Up/Down AND a finite held-to-resolution
    counterfactual P&L is present (spec target A — diagnostic only).
    """
    try:
        if not isinstance(row, dict):
            return None
        cf = row.get("cf_exit_variants")
        if not isinstance(cf, dict):
            return None
        v = _num(cf.get("pnl_if_held_to_resolution"))
        if v is None or row.get("resolved_outcome") not in ("Up", "Down"):
            return None
        return 1 if v > 0 else 0
    except Exception:
        return None


def r_net(row: Any) -> Optional[float]:
    """Stake-normalized, real-fee net $ per unit stake (spec §3.1, fixes I3/I6).

    The ledger's realized_pnl is netted at the DEMO fee (DEMO_FEE_RATE per side), not
    the real ~7.2% Polymarket crypto round-trip (Jan-2026 dynamic taker fee). Under
    martingale, stakes also vary. So:

        r_net = (realized_pnl - wedge) / max(loss_recovery_multiplier, 1.0)

    where `wedge` uses the row's ACTUAL fill_price * contracts when both are present
    (a flat per-$5 wedge is biased optimistic on cheap long-shot fills — exactly where
    the TP mechanic lives). Falls back to a flat STAKE_USD wedge otherwise.
    Returns None if realized_pnl is missing/unparseable. Never raises.
    """
    try:
        if not isinstance(row, dict):
            return None
        rp = _num(row.get("realized_pnl"))
        if rp is None:
            return None
        fill = _num(row.get("fill_price"))
        contracts = _num(row.get("contracts"))
        real_wedge_rate = REAL_RATE - 2 * DEMO_FEE_RATE
        if fill and contracts:
            wedge = real_wedge_rate * fill * contracts
        else:
            wedge = real_wedge_rate * STAKE_USD
        mult = max(_num(row.get("loss_recovery_multiplier")) or 1.0, 1.0)
        return (rp - wedge) / mult
    except Exception:
        return None


def clean(row: Any) -> bool:
    """Martingale / exploration confound filter (spec G5).

    True only for rows that are NOT under loss-recovery and NOT exploration:
    recovery_active is not True, loss_recovery_multiplier <= 1.0, exploration_flag
    in (0, False, None). Non-dict rows are NOT clean. Never raises.
    """
    try:
        if not isinstance(row, dict):
            return False
        rf = row.get("rule_flags")
        if not isinstance(rf, dict):
            rf = {}
        return (
            rf.get("recovery_active") is not True
            and (_num(row.get("loss_recovery_multiplier")) or 1.0) <= 1.0
            and row.get("exploration_flag") in (0, False, None)
        )
    except Exception:
        return False


# ── Task 3: feature accessors + FEATURES catalog (spec §3.1) ────────────────
#
# Every accessor reads ONE row dict and returns float | str | None, never raising.
# The light=False ledger nests the engineered features as:
#     row["context"]["ta"]["features"][<k>]      -> continuous TA features
#     row["context"]["clob"][<side>][<k>]        -> per-side CLOB microstructure
# and keeps the cheap categoricals at the top level (side / vol_bucket / window_sec).


def _safe_dict(v: Any) -> dict:
    """Return v if it is a dict, else an empty dict. Never raises."""
    return v if isinstance(v, dict) else {}


def _ta_feature(key: str) -> Callable[[Any], Optional[float]]:
    """Accessor for a continuous TA feature at context.ta.features[key] -> float|None."""

    def _get(row: Any) -> Optional[float]:
        try:
            ctx = _safe_dict(_safe_dict(row).get("context"))
            feats = _safe_dict(_safe_dict(ctx.get("ta")).get("features"))
            return _num(feats.get(key))
        except Exception:
            return None

    return _get


def _clob_feature(side: str, key: str) -> Callable[[Any], Optional[float]]:
    """Accessor for a per-side CLOB feature at context.clob[side][key] -> float|None."""

    def _get(row: Any) -> Optional[float]:
        try:
            ctx = _safe_dict(_safe_dict(row).get("context"))
            book = _safe_dict(_safe_dict(ctx.get("clob")).get(side))
            return _num(book.get(key))
        except Exception:
            return None

    return _get


def _cat_top(key: str) -> Callable[[Any], Optional[Any]]:
    """Accessor for a top-level categorical field -> the raw value (str/num) or None."""

    def _get(row: Any) -> Optional[Any]:
        try:
            if not isinstance(row, dict):
                return None
            v = row.get(key)
            if v is None or isinstance(v, (dict, list, tuple, set)):
                return None
            # Numeric-categoricals (e.g. window_sec) are coerced to float; the rest
            # (side / vol_bucket) stay as their string label.
            num = _num(v)
            if num is not None and not isinstance(v, str):
                return num
            return v
        except Exception:
            return None

    return _get


# FEATURES: ~40 (path_fn, name, kind) entries — kind in {"cont","cat"} (spec §3.1).
# Continuous TA + per-side CLOB microstructure -> tertile-bucketed.
# Categoricals (side / vol_bucket / window_sec) -> kept as-is.
_TA_CONT = [
    "rsi7", "rsi30", "macd_pct", "macd_signal_pct", "macd_hist_pct",
    "stoch_k", "stoch_d", "stoch_k_30", "bb_pct_b", "bb_bandwidth",
    "ret_1m", "ret_2m", "ret_3m", "ret_5m", "ret_10m", "ret_15m",
    "rv_5", "rv_15", "rv_30",
    "ema9_21_ratio", "price_vs_ema21_pct",
    "volume", "volume_z", "obv", "obv_slope", "atr_pct",
]
_CLOB_CONT_KEYS = ["spread_pct", "l1_imbalance", "depth_ratio", "microprice"]

FEATURES: list[tuple[Callable[[Any], Any], str, str]] = []
for _name in _TA_CONT:
    FEATURES.append((_ta_feature(_name), _name, "cont"))
for _side in ("up", "down"):
    for _k in _CLOB_CONT_KEYS:
        FEATURES.append((_clob_feature(_side, _k), f"clob_{_side}_{_k}", "cont"))
for _cat in ("side", "vol_bucket", "window_sec"):
    FEATURES.append((_cat_top(_cat), _cat, "cat"))

# Fast name -> (path_fn, kind) lookup for feature_value().
_FEATURE_BY_NAME: dict[str, tuple[Callable[[Any], Any], str]] = {
    name: (fn, kind) for (fn, name, kind) in FEATURES
}


def feature_value(row: Any, feat_name: str) -> Optional[Any]:
    """Safe nested read of a named feature on one row -> float | str | None.

    Unknown feature names and malformed rows return None; never raises.
    """
    try:
        entry = _FEATURE_BY_NAME.get(feat_name)
        if entry is None:
            return None
        return entry[0](row)
    except Exception:
        return None


# ── Task 3: walk-forward split + expanding folds + frozen bucketizer ─────────


def _decision_ts(row: Any) -> float:
    """Sort key: the row's decision_ts as a float (missing/malformed -> -inf,
    so unstamped rows sink to the oldest end and never reorder real ones)."""
    if not isinstance(row, dict):
        return float("-inf")
    v = _num(row.get("decision_ts"))
    return v if v is not None else float("-inf")


# Most-recent 30% is sealed as the forward out-of-sample vault (spec §3.5).
_VAULT_FRAC = 0.30
# Boundary embargo: drop the 2 trades straddling each train/test fold cut so a
# leaked-context window can't bridge the wall (spec §3 / leak guard).
_EMBARGO = 2


def _walk_forward_split(rows: Any) -> tuple[list, list]:
    """Time-ordered 70/30 split. Sorts by decision_ts ASCending and seals the
    MOST-RECENT 30% as the out-of-sample vault. NEVER shuffles. Returns
    (discovery_70, oos_vault_30). Degenerate input -> ([], []); never raises.
    """
    try:
        if not isinstance(rows, (list, tuple)) or not rows:
            return ([], [])
        ordered = sorted([r for r in rows if isinstance(r, dict)], key=_decision_ts)
        n = len(ordered)
        if n == 0:
            return ([], [])
        n_vault = int(round(n * _VAULT_FRAC))
        # Keep at least one row on each side once there is more than one row.
        if n >= 2:
            n_vault = min(max(n_vault, 1), n - 1)
        else:
            n_vault = 0
        cut = n - n_vault
        return (ordered[:cut], ordered[cut:])
    except Exception:
        return ([], [])


def _folds(discovery: Any, k: int = 5) -> list[tuple[list, list]]:
    """>=k expanding-window folds over the (already time-ordered) discovery set,
    embargoing _EMBARGO boundary trades between each train segment and its test
    segment. Each fold is (train, test) with every train ts < every test ts.

    Train windows expand monotonically. Degenerate / too-small input -> []; never
    raises.
    """
    try:
        if not isinstance(discovery, (list, tuple)) or not discovery:
            return []
        ordered = sorted([r for r in discovery if isinstance(r, dict)], key=_decision_ts)
        n = len(ordered)
        try:
            k = int(k)
        except (TypeError, ValueError):
            k = 5
        if k < 5:
            k = 5
        # Need at least one train row, _EMBARGO dropped rows, and one test row per
        # fold across k folds — bail safely if the discovery set is too thin.
        min_needed = k + (k * _EMBARGO) + 1
        if n < min_needed:
            return []

        # Split the tail of the set into k contiguous test blocks; train = everything
        # strictly before each block, minus the _EMBARGO rows straddling the cut.
        folds: list[tuple[list, list]] = []
        # Reserve a base train segment, then carve k test blocks from the remainder.
        # First test block starts after an initial train of >=1 row + embargo.
        block = max((n - 1) // (k + 1), 1)
        start = n - block * k
        if start <= _EMBARGO:
            start = _EMBARGO + 1
        for i in range(k):
            test_lo = start + i * block
            test_hi = (start + (i + 1) * block) if i < k - 1 else n
            train_hi = test_lo - _EMBARGO  # drop the _EMBARGO rows before the test
            if train_hi <= 0 or test_lo >= test_hi:
                continue
            train = ordered[:train_hi]
            test = ordered[test_lo:test_hi]
            if train and test:
                folds.append((train, test))
        return folds
    except Exception:
        return []


def bucketize(train_rows: Any, feat: tuple[Callable[[Any], Any], str, str]) -> Callable[[Any], Optional[str]]:
    """Freeze a value -> bucket-label mapping fit ONLY on the train segment (spec I5).

    Continuous features -> tertile labels ("<name>:low|mid|high") from train-only
    tertile cut-points; the SAME frozen cut-points are reused unchanged on test/vault
    rows (so appended test data can never move a boundary — leak guard T8).
    Categorical features -> the raw value as its own label ("<name>:<value>").

    Returns a closure `bucket(row) -> str | None` (None when the row's feature value
    is missing). Never raises (degenerate train -> a closure that returns None/labels
    safely).
    """
    try:
        path_fn, name, kind = feat[0], feat[1], feat[2]
    except Exception:
        return lambda row: None

    if kind == "cat":
        def _bucket_cat(row: Any) -> Optional[str]:
            try:
                v = path_fn(row)
                if v is None:
                    return None
                return f"{name}:{v}"
            except Exception:
                return None

        return _bucket_cat

    # Continuous: freeze tertile cut-points on the TRAIN segment only.
    try:
        vals = []
        if isinstance(train_rows, (list, tuple)):
            for r in train_rows:
                v = path_fn(r)
                if v is not None:
                    fv = _num(v)
                    if fv is not None:
                        vals.append(fv)
        q33, q66 = es.tertiles(vals)
    except Exception:
        q33 = q66 = 0.0

    def _bucket_cont(row: Any) -> Optional[str]:
        try:
            v = path_fn(row)
            if v is None:
                return None
            fv = _num(v)
            if fv is None:
                return None
            if fv <= q33:
                return f"{name}:low"
            if fv <= q66:
                return f"{name}:mid"
            return f"{name}:high"
        except Exception:
            return None

    return _bucket_cont


# ── Task 4: slice evaluator — gates G0–G5 (spec §3.2–3.7, §3.8) ──────────────
#
# Statistical heart. Given a slice-membership predicate `mask_fn`, score the slice
# against ITS COMPLEMENT (never a constant) with the adversarial fixes intact:
#   * slice-vs-complement two-proportion (baseline = discovery complement) — G2/G1
#   * DAY-BLOCK permutation p-value (NOT a plain binomial) — G2
#   * effective-n via the design-effect 1+(m̄−1)ρ — G0
#   * economic MASTER gate with a day-block-bootstrapped 5th-pct tail + losers floor — G3
#   * regime stability across folds / UTC days / vol-buckets — G4
#   * clean() martingale-confound survival — G5
# Every public entry never raises: malformed input -> an all-False safe verdict.

# Regime / stability / economic-gate constants (spec §3.6, §3.7).
WORST_FOLD_MIN_NET = -0.05      # worst calendar fold mean(r_net) must exceed this
TOP_DAY_MAX_FRAC = 0.40        # a single UTC day may hold < 40% of |slice P&L|
STABILITY_FOLDS = 4            # 4 contiguous calendar folds; sign must hold in >=3
STABILITY_FOLDS_MIN_OK = 3
VOL_REGIMES_MIN_OK = 2         # edge positive in >=2 of 3 vol-buckets
N_LOSERS_MIN = 10             # the loss tail must actually be sampled (spec §3.6)
MIN_SLICE_DAYBLOCKS = 3      # an edge confined to <3 UTC days is autocorrelation,
                             # never a real edge (spec §3.3 — the dominant FP vector)
_DAY_SECONDS = 86400.0
_PERM_ITERS = 1000
_BOOT_ITERS = 1000
_PERM_SEED = 1234567
_BOOT_SEED = 7654321


def _day_key(row: Any) -> Any:
    """UTC-day block key for a row (floor(decision_ts / 86400)). Unstamped rows
    fall into a single sentinel block. Never raises."""
    ts = _decision_ts(row)
    if ts == float("-inf") or not math.isfinite(ts):
        return "_nodate_"
    return int(ts // _DAY_SECONDS)


def _label_fn_for(target: str) -> Callable[[Any], Optional[int]]:
    """Binary label extractor for a target.

    * "tp_reach"   -> y_tp (1 iff realized TP exit).
    * "abstention" -> 1 iff the trade was NOT a TP AND held-to-resolution lost,
                       i.e. skipping it would have been correct. None when the
                       directional counterfactual is unavailable.
    """
    if target == "abstention":
        def _lab(row: Any) -> Optional[int]:
            yd = y_dir(row)
            if yd is None:
                return None
            # "correct abstention" = holding loses (yd == 0) and it wasn't a TP.
            return 1 if (yd == 0 and y_tp(row) == 0) else 0
        return _lab
    # default / "tp_reach"
    return lambda row: y_tp(row)


def _intraclass_rho(labels: list[float], day_keys: list[Any]) -> float:
    """One-way-ANOVA intraclass correlation of a 0/1 (or real) label across day
    blocks, clamped to [0, 1]. Drives the design-effect for effective-n. The more
    same-day rows move together, the closer ρ -> 1 (and effective-n collapses).
    Degenerate -> 0.0; never raises.
    """
    try:
        n = len(labels)
        if n < 2 or len(day_keys) != n:
            return 0.0
        groups: dict[Any, list[float]] = {}
        for lab, dk in zip(labels, day_keys):
            groups.setdefault(dk, []).append(float(lab))
        k = len(groups)
        if k < 2:
            # everything in one block -> maximally correlated.
            return 1.0
        grand = sum(labels) / n
        ss_between = 0.0
        ss_within = 0.0
        for vals in groups.values():
            m = len(vals)
            gm = sum(vals) / m
            ss_between += m * (gm - grand) ** 2
            for v in vals:
                ss_within += (v - gm) ** 2
        ms_between = ss_between / (k - 1)
        ms_within = ss_within / (n - k) if n > k else 0.0
        # Mean cluster size correction (m0).
        sum_m2 = sum(len(v) ** 2 for v in groups.values())
        m0 = (n - sum_m2 / n) / (k - 1)
        if m0 <= 0:
            return 0.0
        denom = ms_between + (m0 - 1.0) * ms_within
        if denom <= 0:
            return 0.0
        rho = (ms_between - ms_within) / denom
        return min(max(rho, 0.0), 1.0)
    except Exception:
        return 0.0


def _effective_n(n_raw: int, labels: list[float], day_keys: list[Any]) -> float:
    """Design-effect-adjusted effective sample size: n_eff = n_raw / (1+(m̄−1)ρ).
    m̄ = mean rows per day-block, ρ = intraclass correlation. Never raises.
    """
    try:
        if n_raw <= 0:
            return 0.0
        uniq_days = len(set(day_keys)) or 1
        mbar = n_raw / uniq_days
        rho = _intraclass_rho(labels, day_keys)
        design_effect = 1.0 + (mbar - 1.0) * rho
        if design_effect <= 0:
            design_effect = 1.0
        return float(n_raw) / design_effect
    except Exception:
        return 0.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _day_block_lift_pvalue(
    disc: list, mask_fn, label_fn, iters: int = _BOOT_ITERS, seed: int = _PERM_SEED
) -> float:
    """DAY-BLOCK bootstrap one-sided p-value for "slice mean(label) > complement
    mean(label)" on a feature slice that may appear WITHIN every day.

    Unlike a whole-day reassignment permutation, this resamples whole UTC DAY
    BLOCKS with replacement (keeping each day's slice/complement mix intact, so
    same-day autocorrelation is preserved) and reports the fraction of replicates
    in which the slice no longer beats its complement. An edge that lives in only
    one or two lucky day-blocks collapses here (those days are frequently absent
    from a replicate), while a genuine every-day feature edge survives.

    Deterministic for a fixed seed. Degenerate input -> 1.0; never raises.
    """
    try:
        # Group each labeled row's (in_slice, label) by its day-block.
        by_day: dict[Any, list[tuple[bool, float]]] = {}
        for r in disc:
            lab = label_fn(r)
            if lab is None:
                continue
            dk = _day_key(r)
            by_day.setdefault(dk, []).append(
                (_safe_call_mask(mask_fn, r), float(lab))
            )
        day_order = list(by_day.keys())
        n_days = len(day_order)
        if n_days < 2:
            return 1.0

        def _lift(blocks: list) -> Optional[float]:
            s_sum = s_cnt = c_sum = c_cnt = 0.0
            for dk in blocks:
                for is_in, lab in by_day[dk]:
                    if is_in:
                        s_sum += lab
                        s_cnt += 1
                    else:
                        c_sum += lab
                        c_cnt += 1
            if s_cnt == 0 or c_cnt == 0:
                return None
            return s_sum / s_cnt - c_sum / c_cnt

        observed = _lift(day_order)
        if observed is None:
            return 1.0

        rng = random.Random(seed)
        try:
            iters = int(iters)
        except (TypeError, ValueError):
            iters = _BOOT_ITERS
        if iters <= 0:
            iters = _BOOT_ITERS
        le = 0  # replicates where the slice did NOT beat its complement
        for _ in range(iters):
            sample = [day_order[rng.randrange(n_days)] for _ in range(n_days)]
            lift = _lift(sample)
            if lift is None or lift <= 0.0:
                le += 1
        return (le + 1) / (iters + 1)
    except Exception:
        return 1.0


def _two_prop_lift(in_rows: list, comp_rows: list, label_fn) -> tuple:
    """(k1, n1, k2, n2, hit_rate_pct, baseline_pct, lift_pct) for slice vs
    complement on a binary label, skipping rows whose label is None. Never raises.
    """
    try:
        k1 = n1 = k2 = n2 = 0
        for r in in_rows:
            lab = label_fn(r)
            if lab is None:
                continue
            n1 += 1
            k1 += 1 if lab else 0
        for r in comp_rows:
            lab = label_fn(r)
            if lab is None:
                continue
            n2 += 1
            k2 += 1 if lab else 0
        hit = (k1 / n1 * 100.0) if n1 else 0.0
        base = (k2 / n2 * 100.0) if n2 else 0.0
        return (k1, n1, k2, n2, hit, base, hit - base)
    except Exception:
        return (0, 0, 0, 0, 0.0, 0.0, 0.0)


def _safe_call_mask(mask_fn, row) -> bool:
    try:
        return bool(mask_fn(row))
    except Exception:
        return False


def _empty_slice_result() -> dict:
    """The all-False safe verdict returned on degenerate / malformed input."""
    return {
        "n_eff": 0.0,
        "fire_rate": 0.0,
        "lift_pct": 0.0,
        "hit_rate_pct": 0.0,
        "baseline_pct": 0.0,
        "wilson_ok": False,
        "pvalue": 1.0,
        "n_dayblocks": 0,
        "r_net_mean": 0.0,
        "r_net_p5_boot": 0.0,
        "r_net_p95_boot": 0.0,
        "n_losers": 0,
        "n_winners": 0,
        "dsr": 0.0,
        "stability": {
            "folds_ok": 0,
            "folds_total": 0,
            "worst_fold_net": 0.0,
            "top_day_frac": 1.0,
            "vol_regimes_ok": 0,
            "vol_regimes_total": 0,
            "ok": False,
        },
        "clean_survives": False,
        "passes_g0": False,
        "passes_g1": False,
        "passes_g2": False,
        "passes_g3": False,
        "passes_g4": False,
        "passes_g5": False,
    }


def _stability(in_rows: list, label_fn, econ_sign: float = 1.0) -> dict:
    """Regime stability over the in-slice rows (spec §3.7):
      * sign of mean(r_net) holds in >=3 of 4 contiguous calendar folds,
      * worst-fold mean(r_net) > WORST_FOLD_MIN_NET,
      * single top UTC day holds < TOP_DAY_MAX_FRAC of |slice P&L|,
      * edge positive in >=2 of 3 vol-buckets.
    `econ_sign` orients the economics: +1 for tp_reach (edge = profitable), −1 for
    abstention (edge = reliably COSTLY, so the favorable sign is negative r_net).
    Never raises.
    """
    out = {
        "folds_ok": 0,
        "folds_total": 0,
        "worst_fold_net": 0.0,
        "top_day_frac": 1.0,
        "vol_regimes_ok": 0,
        "vol_regimes_total": 0,
        "ok": False,
    }
    try:
        ordered = sorted([r for r in in_rows if isinstance(r, dict)], key=_decision_ts)
        nets = [(r, r_net(r)) for r in ordered]
        nets = [(r, v) for (r, v) in nets if v is not None]
        if not nets:
            return out

        # ── contiguous calendar folds (by time order, STABILITY_FOLDS chunks) ──
        n = len(nets)
        kfolds = min(STABILITY_FOLDS, n)
        fold_means: list[float] = []
        if kfolds >= 1:
            size = n / kfolds
            for f in range(kfolds):
                lo = int(round(f * size))
                hi = int(round((f + 1) * size)) if f < kfolds - 1 else n
                chunk = [v for (_, v) in nets[lo:hi]]
                if chunk:
                    fold_means.append(_mean(chunk))
        out["folds_total"] = len(fold_means)
        out["folds_ok"] = sum(1 for m in fold_means if econ_sign * m > 0.0)
        out["worst_fold_net"] = min((econ_sign * m for m in fold_means), default=0.0)

        # ── top single UTC day as a fraction of |slice P&L| ──
        by_day: dict[Any, float] = {}
        total_abs = 0.0
        for (r, v) in nets:
            dk = _day_key(r)
            by_day[dk] = by_day.get(dk, 0.0) + v
            total_abs += abs(v)
        if total_abs > 0:
            out["top_day_frac"] = max(abs(s) for s in by_day.values()) / total_abs
        else:
            out["top_day_frac"] = 1.0

        # ── vol-bucket regimes: edge (mean r_net) positive in >=2 of 3 ──
        by_vol: dict[Any, list[float]] = {}
        for (r, v) in nets:
            vb = r.get("vol_bucket") if isinstance(r, dict) else None
            by_vol.setdefault(vb, []).append(v)
        vol_means = {vb: _mean(vs) for vb, vs in by_vol.items() if vs}
        out["vol_regimes_total"] = len(vol_means)
        out["vol_regimes_ok"] = sum(1 for m in vol_means.values() if econ_sign * m > 0.0)

        out["ok"] = (
            out["folds_total"] >= STABILITY_FOLDS_MIN_OK
            and out["folds_ok"] >= STABILITY_FOLDS_MIN_OK
            and out["worst_fold_net"] > WORST_FOLD_MIN_NET
            and out["top_day_frac"] < TOP_DAY_MAX_FRAC
            and out["vol_regimes_total"] >= 1
            and out["vol_regimes_ok"] >= min(VOL_REGIMES_MIN_OK, out["vol_regimes_total"])
        )
        return out
    except Exception:
        return out


def _evaluate_slice(disc_rows: Any, vault_rows: Any, mask_fn: Any, target: str) -> dict:
    """Score one slice against ITS COMPLEMENT with the full G0–G5 gate battery.

    Returns a dict with n_eff / fire_rate / lift_pct / hit_rate_pct / baseline_pct
    / wilson_ok / pvalue (DAY-BLOCK permutation, NOT binomial) / r_net_mean /
    r_net_p5_boot / n_losers / dsr / stability{...} / clean_survives / passes_g0..g5.

    Never raises — any malformed/degenerate input yields an all-False safe verdict.
    """
    try:
        if not isinstance(disc_rows, (list, tuple)):
            disc_rows = []
        if not isinstance(vault_rows, (list, tuple)):
            vault_rows = []
        disc = [r for r in disc_rows if isinstance(r, dict)]
        vault = [r for r in vault_rows if isinstance(r, dict)]
        if not disc:
            return _empty_slice_result()

        label_fn = _label_fn_for(target)
        res = _empty_slice_result()

        # ── partition discovery into slice / complement ──
        in_rows = [r for r in disc if _safe_call_mask(mask_fn, r)]
        comp_rows = [r for r in disc if not _safe_call_mask(mask_fn, r)]
        n_slice = len(in_rows)
        n_disc = len(disc)
        res["fire_rate"] = (n_slice / n_disc) if n_disc else 0.0

        # ── binary-label slice-vs-COMPLEMENT (discovery) ──
        k1, n1, k2, n2, hit, base, lift = _two_prop_lift(in_rows, comp_rows, label_fn)
        res["hit_rate_pct"] = hit
        res["baseline_pct"] = base
        res["lift_pct"] = lift
        lo, hi = es.wilson_bounds(k1, n1)
        # Wilson lower bound of the slice clears the complement point estimate.
        res["wilson_ok"] = bool(n1 > 0 and n2 > 0 and lo > (k2 / n2 if n2 else 1.0))

        # ── per-row slice-label stream (for effective-n / day-block count) ──
        slice_labels: list[float] = []
        slice_days: list[Any] = []
        for r in in_rows:
            lab = label_fn(r)
            if lab is None:
                continue
            slice_labels.append(float(lab))
            slice_days.append(_day_key(r))
        # DAY-BLOCK bootstrap p-value for the lift (slice mean > complement mean),
        # resampling whole UTC day-blocks so same-day autocorrelation is preserved.
        # This is the GATE — never the plain row-level binomial (spec §3.3).
        res["pvalue"] = _day_block_lift_pvalue(
            disc, mask_fn, label_fn, iters=_PERM_ITERS, seed=_PERM_SEED
        )

        # Distinct UTC day-blocks the slice actually spans — an edge confined to a
        # handful of contiguous days is autocorrelation, not signal (spec §3.3).
        n_slice_dayblocks = len(set(slice_days))
        res["n_dayblocks"] = n_slice_dayblocks

        # ── effective-n (design-effect adjusted) on the slice's label stream ──
        res["n_eff"] = _effective_n(len(slice_labels), slice_labels, slice_days)

        # ── economics: r_net stream over the slice (real-fee, stake-normalized) ──
        r_in = [r_net(r) for r in in_rows]
        r_in = [v for v in r_in if v is not None]
        res["r_net_mean"] = _mean(r_in)
        res["n_losers"] = sum(1 for v in r_in if v < 0.0)
        res["n_winners"] = sum(1 for v in r_in if v > 0.0)

        # day-block bootstrap of mean(r_net) -> 5th / 95th-percentile tails.
        # tp_reach gates on the lower tail (p5 > 0); abstention on the upper tail
        # (p95 < 0 -> even the best resample is still costly).
        econ_days = [_day_key(r) for r in in_rows if r_net(r) is not None]
        boots = es.day_block_bootstrap(
            r_in, econ_days, _mean, iters=_BOOT_ITERS, seed=_BOOT_SEED
        )
        if boots:
            s = sorted(boots)
            res["r_net_p5_boot"] = s[int(0.05 * (len(s) - 1))]
            res["r_net_p95_boot"] = s[int(0.95 * (len(s) - 1))]
        else:
            res["r_net_p5_boot"] = 0.0
            res["r_net_p95_boot"] = 0.0

        # Deflated Sharpe on the DAY-BLOCK-AVERAGED r_net stream (one return per UTC
        # day), haircut for the honest per-scan trial count. Aggregating to the day
        # level is the day-block-resampled DSR the spec asks for (§3.3): it both
        # respects same-day autocorrelation and collapses an edge that lives in only
        # a few high-variance days, while a steady every-day edge clears DSR_MIN.
        day_net: dict[Any, list[float]] = {}
        for r in in_rows:
            v = r_net(r)
            if v is None:
                continue
            day_net.setdefault(_day_key(r), []).append(v)
        day_means = [_mean(vs) for vs in day_net.values() if vs]
        res["dsr"] = es.deflated_sharpe(day_means, n_trials=max(len(FEATURES) * 6, 1))

        # ── stability (folds / top-day / vol regimes) ──
        # abstention's favorable economics are NEGATIVE -> flip the sign.
        econ_sign = -1.0 if target == "abstention" else 1.0
        res["stability"] = _stability(in_rows, label_fn, econ_sign)

        # ── clean() survival: edge holds on the martingale/exploration-free subset ──
        clean_in = [r for r in in_rows if clean(r)]
        clean_comp = [r for r in comp_rows if clean(r)]
        ck1, cn1, ck2, cn2, c_hit, c_base, c_lift = _two_prop_lift(
            clean_in, clean_comp, label_fn
        )
        clean_r = [r_net(r) for r in clean_in]
        clean_r = [v for v in clean_r if v is not None]
        clean_net = _mean(clean_r)
        # The slice must retain BOTH its directional lift AND its economics (the
        # right SIGN per target) once the confounded rows are removed, on a
        # non-trivial clean sample.
        clean_econ_ok = (
            clean_net <= ECON_ABSTAIN_NET if target == "abstention"
            else clean_net >= ECON_MIN_NET
        )
        res["clean_survives"] = bool(
            cn1 >= max(1, int(0.5 * N_SLICE_MIN_EFFECTIVE))
            and c_lift >= MIN_RAW_LIFT_PTS
            and clean_econ_ok
        )

        # ── forward-OOS slice-vs-complement on the vault (G1) ──
        v_in = [r for r in vault if _safe_call_mask(mask_fn, r)]
        v_comp = [r for r in vault if not _safe_call_mask(mask_fn, r)]
        vk1, vn1, vk2, vn2, v_hit, v_base, v_lift = _two_prop_lift(
            v_in, v_comp, label_fn
        )
        vault_p = es.two_proportion_p(vk1, vn1, vk2, vn2, alternative="greater")

        # ── gates ──
        res["passes_g0"] = bool(
            res["n_eff"] >= N_SLICE_MIN_EFFECTIVE
            and res["fire_rate"] >= FIRE_RATE_MIN
            and res["lift_pct"] >= MIN_RAW_LIFT_PTS
        )
        res["passes_g1"] = bool(
            vn1 > 0 and vn2 > 0 and v_lift >= MIN_RAW_LIFT_PTS and vault_p <= BH_Q
        )
        res["passes_g2"] = bool(
            res["pvalue"] <= BH_Q
            and res["dsr"] >= DSR_MIN
            and res["wilson_ok"]
            and n_slice_dayblocks >= MIN_SLICE_DAYBLOCKS
        )
        if target == "abstention":
            # E edge = the slice is genuinely COSTLY (skipping it is the win):
            # reliably-negative economics, the opposite (winning) tail actually
            # sampled, and even the best day-block resample still loses money.
            res["passes_g3"] = bool(
                res["r_net_mean"] <= ECON_ABSTAIN_NET
                and res["r_net_mean"] < 0.0
                and res["r_net_p95_boot"] < 0.0
                and res["n_winners"] >= N_LOSERS_MIN
            )
        else:
            res["passes_g3"] = bool(
                res["r_net_mean"] >= ECON_MIN_NET
                and res["r_net_mean"] > 0.0
                and res["r_net_p5_boot"] > 0.0
                and res["n_losers"] >= N_LOSERS_MIN
            )
        res["passes_g4"] = bool(res["stability"].get("ok"))
        res["passes_g5"] = bool(res["clean_survives"])
        return res
    except Exception:
        return _empty_slice_result()


# ── Task 6: detect_edges orchestrator + state machine + G6 persistence ───────
#
# Pure-Python over a list of light=False ledger rows. Filters to settled & labeled
# rows; below TOTAL_MIN -> "collecting". Otherwise enumerates B (tp_reach) and E
# (abstention) feature-slices, scores each against its complement with the full
# G0-G5 gate battery, applies per-scan BH-FDR over the honest hypothesis count m,
# then applies G6 forward-time persistence (edge_persistence) so a single scan can
# reach at most "forming" — never "confirmed". The directional (A) diagnostic only
# sets directional_note_he, never a card, never confirmed (spec §3.8).
#
# NEVER RAISES: any malformed / empty input returns a safe "collecting" response.

try:  # the sidecar is optional at import time (recording-only; never trading code)
    import edge_persistence as _ep
except Exception:  # pragma: no cover - import guard
    _ep = None


# A row is analyzable only once it is settled AND labeled WIN/LOSS (mirrors the
# audit_tracker labels_only filter: settlement_status IN ('WIN','LOSS')).
_SETTLED_LABELS = ("WIN", "LOSS")


def _is_settled_labeled(row: Any) -> bool:
    try:
        return isinstance(row, dict) and row.get("settlement_status") in _SETTLED_LABELS
    except Exception:
        return False


def _slice_mask_for(bucket_fn: Callable[[Any], Optional[str]], label: str) -> Callable[[Any], bool]:
    """A membership predicate: the row's frozen bucket equals `label`. Never raises."""

    def _mask(row: Any) -> bool:
        try:
            return bucket_fn(row) == label
        except Exception:
            return False

    return _mask


def _enumerate_slices(disc_rows: list) -> list[tuple[str, Callable[[Any], bool]]]:
    """Every (slice_key, mask_fn) candidate: each FEATURE bucketized (frozen on the
    DISCOVERY set as the train segment — leak guard) yields one mask per bucket label
    seen in discovery. slice_key is a stable "<feature>:<bucket>" string. Never raises.
    """
    slices: list[tuple[str, Callable[[Any], bool]]] = []
    try:
        for feat in FEATURES:
            try:
                bucket_fn = bucketize(disc_rows, feat)
            except Exception:
                continue
            labels: set[str] = set()
            for r in disc_rows:
                try:
                    lbl = bucket_fn(r)
                except Exception:
                    lbl = None
                if isinstance(lbl, str):
                    labels.add(lbl)
            for lbl in sorted(labels):
                slices.append((lbl, _slice_mask_for(bucket_fn, lbl)))
    except Exception:
        return slices
    return slices


def _max_decision_ts(rows: list) -> float:
    """Frozen forward-position marker: the max decision_ts across settled rows.
    Empty -> 0.0; never raises."""
    try:
        best = 0.0
        seen = False
        for r in rows:
            ts = _decision_ts(r)
            if math.isfinite(ts):
                if not seen or ts > best:
                    best = ts
                    seen = True
        return best if seen else 0.0
    except Exception:
        return 0.0


def _confidence_for(res: dict, confirmations: int) -> str:
    """Plain-language confidence label. `high` requires the full gate battery AND
    forward confirmation; otherwise `מבינוני`/`נמוך` per how much cleared."""
    try:
        gates = sum(
            1
            for g in ("passes_g0", "passes_g1", "passes_g2", "passes_g3", "passes_g4", "passes_g5")
            if res.get(g)
        )
        if gates == 6 and confirmations >= MIN_CONFIRMATIONS:
            return "high"
        if gates >= 4:
            return "medium"
        return "low"
    except Exception:
        return "low"


def _setup_he(slice_key: str, edge_type: str) -> str:
    """Plain-Hebrew setup description for a candidate card. Never raises."""
    kind = "פגיעה ב-TP" if edge_type == "tp_reach" else "הימנעות (לדלג)"
    try:
        return f"כשמתקיים: {slice_key} → {kind}"
    except Exception:
        return kind


def _build_card(slice_key: str, edge_type: str, res: dict, confirmations: int,
                oos_confirmed: bool) -> dict:
    """Build one EdgeCard (spec §4 shape). JSON-safe; never raises."""
    try:
        sample_n = int(round(res.get("n_eff", 0.0)))
        more = max(MIN_CONFIRMATIONS - max(int(confirmations), 0), 0)
        return {
            "setup_he": _setup_he(slice_key, edge_type),
            "edge_type": edge_type,
            "hit_rate_pct": round(float(res.get("hit_rate_pct", 0.0)), 1),
            "baseline_pct": round(float(res.get("baseline_pct", 0.0)), 1),
            "lift_pct": round(float(res.get("lift_pct", 0.0)), 1),
            "sample_n": sample_n,
            "net_dollars_per_trade": round(float(res.get("r_net_mean", 0.0)), 3),
            "oos_confirmed": bool(oos_confirmed),
            "confirmations": int(max(confirmations, 0)),
            "confidence": _confidence_for(res, confirmations),
            "more_trades_to_confirm": int(more * CONFIRM_SPACING_TRADES),
            "slice_key": slice_key,
        }
    except Exception:
        return {
            "setup_he": _setup_he(slice_key, edge_type),
            "edge_type": edge_type,
            "hit_rate_pct": 0.0,
            "baseline_pct": 0.0,
            "lift_pct": 0.0,
            "sample_n": 0,
            "net_dollars_per_trade": 0.0,
            "oos_confirmed": False,
            "confirmations": 0,
            "confidence": "low",
            "more_trades_to_confirm": MIN_CONFIRMATIONS * CONFIRM_SPACING_TRADES,
            "slice_key": slice_key,
        }


def _diagnose_directional(settled: list) -> Optional[str]:
    """A (DIAGNOSTIC-ONLY): is held-to-resolution directionally informative? Returns
    a plain-Hebrew note labeled "(לפני עמלות אמיתיות)" when a side's hold-win-rate is
    notably off 50%, else None. NEVER a card / confirmed / nudge (spec §3.8). Never raises.
    """
    try:
        by_side: dict[str, list[int]] = {"Up": [], "Down": []}
        for r in settled:
            yd = y_dir(r)
            if yd is None:
                continue
            side = r.get("side") if isinstance(r, dict) else None
            if side in ("Up", "Down"):
                by_side[side].append(yd)
        notes = []
        for side, ys in by_side.items():
            if len(ys) < 100:
                continue
            wr = 100.0 * sum(ys) / len(ys)
            if abs(wr - 50.0) >= 8.0:
                notes.append(f"{side}: {wr:.0f}% החזקה-עד-הכרעה (n={len(ys)})")
        if not notes:
            return None
        return (
            "אבחון כיווני (לפני עמלות אמיתיות): "
            + " · ".join(notes)
            + " — מידע בלבד, לא איתות מסחר."
        )
    except Exception:
        return None


def _empty_response(state: str, trades_collected: int, note: str) -> dict:
    """A safe EdgeResponse with no candidate (spec §4 shape)."""
    return {
        "state": state,
        "trades_collected": int(trades_collected),
        "trades_min_needed": TOTAL_MIN,
        # The per-slice effective gate (what an EdgeCard's sample_n is measured
        # against in the mayNudgeAutonomy guard — fix M1).
        "trades_min_needed_in_slice": int(N_SLICE_MIN_EFFECTIVE),
        # The honest progress denominator for the UI (fix M4): to expect
        # N_SLICE_MIN effective samples in a >=5%-fire slice you need this many
        # TOTAL labeled trades.
        "trades_min_total_for_slice": int(round(N_SLICE_MIN_EFFECTIVE / FIRE_RATE_MIN)),
        "best_candidate": None,
        "candidates": [],
        "directional_note_he": None,
        "note": note,
    }


_COLLECTING_NOTE = (
    "ממשיכים לאסוף נתונים — 'edge' נחשב אמיתי רק אחרי מבחן קדימה (out-of-sample בזמן אמת), "
    "תיקון לריבוי-בדיקות, וסף רווחיות אחרי עמלות אמיתיות (~7-8%)."
)


def detect_edges(rows: Any, *, config: Any = None) -> dict:
    """Top-level Edge-Watcher verdict (spec §3.8). NEVER raises.

    Filters to settled & labeled rows; below TOTAL_MIN -> "collecting". Otherwise
    runs the B (tp_reach) + E (abstention) slice scans + the A directional diagnostic,
    applies per-scan BH-FDR over the honest hypothesis count m, then G6 forward-time
    persistence. A single scan can reach at most "forming"; "confirmed" requires
    >= MIN_CONFIRMATIONS spaced forward confirmations. Ambiguity collapses to the
    lower (safer) state.
    """
    try:
        if not isinstance(rows, (list, tuple)):
            return _empty_response("collecting", 0, _COLLECTING_NOTE)

        settled = [r for r in rows if _is_settled_labeled(r)]
        n_total = len(settled)

        # Allow a config override of the TP definition target (Task 7 passes
        # take_profit_pct); the constant is the default. We only read it defensively.
        if isinstance(config, dict):
            pass  # take_profit_pct is informational; gates use the booked exit_type.

        if n_total < TOTAL_MIN:
            return _empty_response("collecting", n_total, _COLLECTING_NOTE)

        directional_note = _diagnose_directional(settled)

        disc, vault = _walk_forward_split(settled)
        if not disc:
            return _empty_response("watching", n_total, _COLLECTING_NOTE)

        slices = _enumerate_slices(disc)
        max_ts = _max_decision_ts(settled)

        # ── score every (target, slice): collect raw day-block p-values for BH-FDR ──
        scored: list[dict] = []
        pvals: list[float] = []
        for target in ("tp_reach", "abstention"):
            for slice_key, mask_fn in slices:
                res = _evaluate_slice(disc, vault, mask_fn, target)
                # Cheap pre-screen: a slice that doesn't even clear the raw data /
                # lift floor can't be a candidate — skip it from the m count too,
                # exactly as the trading layer would never test it (honest m).
                if not res.get("passes_g0"):
                    continue
                entry = {
                    "target": target,
                    "slice_key": f"{target}|{slice_key}",
                    "res": res,
                    "mask_fn": mask_fn,
                }
                scored.append(entry)
                pvals.append(float(res.get("pvalue", 1.0)))

        # Honest per-scan multiplicity m == number of G0-passing hypotheses tested.
        m = len(scored)
        if _ep is not None:
            try:
                _ep.record_scan(m)
            except Exception:
                pass

        reject = es.bh_fdr(pvals, q=BH_Q) if pvals else []

        candidates: list[dict] = []
        best: Optional[dict] = None
        any_forming = False

        for i, entry in enumerate(scored):
            res = entry["res"]
            slice_key = entry["slice_key"]
            # ALL statistical + economic gates (G1-G5) AND BH-FDR survival (part of G2).
            bh_ok = bool(reject[i]) if i < len(reject) else False
            stat_ok = bool(
                res.get("passes_g1")
                and res.get("passes_g2")
                and res.get("passes_g3")
                and res.get("passes_g4")
                and res.get("passes_g5")
                and bh_ok
            )

            # ── G6 forward-time persistence (the live-safety gate) ──
            if stat_ok:
                if _ep is not None:
                    try:
                        confirmations = _ep.bump_confirmation(slice_key, max_ts)
                    except Exception:
                        confirmations = 1
                else:
                    confirmations = 1
            else:
                if _ep is not None:
                    try:
                        _ep.reset_confirmation(slice_key)
                    except Exception:
                        pass
                confirmations = 0

            oos_confirmed = bool(stat_ok and confirmations >= MIN_CONFIRMATIONS)
            # slice_key is "<target>|<feature>:<bucket>"; the card's setup uses the
            # human-readable "<feature>:<bucket>" tail.
            feature_label = slice_key.split("|", 1)[1] if "|" in slice_key else slice_key
            card = _build_card(feature_label, entry["target"], res, confirmations, oos_confirmed)
            card["slice_key"] = slice_key

            if stat_ok:
                any_forming = True
                candidates.append(card)
                if best is None or _card_rank(card) > _card_rank(best):
                    best = card

        # ── state machine (spec §3.8) — ambiguity collapses to the safer state ──
        if best is not None and best.get("oos_confirmed") and best.get("confidence") == "high" \
                and best.get("confirmations", 0) >= MIN_CONFIRMATIONS:
            state = "confirmed"
        elif any_forming:
            state = "forming"
            best = None  # forming has a preliminary signal but NO confirmed best card
        else:
            state = "watching"

        # In forming we still surface the cards (as unconfirmed), but best_candidate is
        # reserved for confirmed (the only state the UI nudges from).
        candidates_out = candidates if state in ("forming", "confirmed") else []

        note = _STATE_NOTES.get(state, _COLLECTING_NOTE)
        resp = _empty_response(state, n_total, note)
        resp["candidates"] = candidates_out
        resp["best_candidate"] = best if state == "confirmed" else None
        resp["directional_note_he"] = directional_note
        return resp
    except Exception as e:  # the whole watcher must never break the endpoint
        print(f"[edge_watcher] detect_edges failed: {e!r}", flush=True)
        return _empty_response("collecting", 0, _COLLECTING_NOTE)


def _card_rank(card: dict) -> tuple:
    """Rank candidates: confirmed-and-high first, then by confirmations, net $, lift."""
    try:
        return (
            1 if card.get("oos_confirmed") else 0,
            int(card.get("confirmations", 0)),
            float(card.get("net_dollars_per_trade", 0.0)),
            float(card.get("lift_pct", 0.0)),
        )
    except Exception:
        return (0, 0, 0.0, 0.0)


_STATE_NOTES = {
    "collecting": _COLLECTING_NOTE,
    "watching": (
        "מספיק נתונים, אבל אף מועמד לא עובר אפילו את הסף המקדים. הכול תקין — אל תפעיל "
        "אוטונומיה עכשיו, אין מה להפעיל."
    ),
    "forming": (
        "סימן מקדים ל-edge — עדיין לא מאושר. אל תפעל על סמך זה: צריך עוד עסקאות ומבחן "
        "קדימה שטרם עבר."
    ),
    "confirmed": (
        "סימן ל-edge שעבר את כל הבדיקות (מבחן קדימה, תיקון ריבוי-בדיקות, ורווחיות אחרי "
        "עמלות אמיתיות) — שקול להפעיל אוטונומיה."
    ),
}
