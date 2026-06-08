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
from typing import Any, Callable, Optional

import edge_stats as es


# ── Constants: single source of truth (spec §3.1, §7) ───────────────────────
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
    the real ~3-4% Polymarket round-trip. Under martingale, stakes also vary. So:

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
