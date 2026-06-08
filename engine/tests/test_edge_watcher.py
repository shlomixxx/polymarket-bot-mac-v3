"""Tests for engine/edge_watcher.py.

Bare imports (engine/ is on sys.path via conftest.py) — match the existing suite.

Task 2 scope: the four row extractors (y_tp / y_dir / r_net / clean) + the _num
helper. Every extractor must tolerate missing / None / malformed fields without
raising (spec invariant: NEVER RAISES).
"""

import math

import edge_watcher as ew


# ── constants are the single source of truth (spec §3.1, §7) ────────────────
def test_constants_present_and_sane():
    assert ew.TP_PCT == 18.0
    assert ew.REAL_RATE == 0.035
    assert ew.DEMO_FEE_RATE == 0.002
    assert ew.STAKE_USD == 5.0
    assert ew.TOTAL_MIN == 800
    assert ew.N_SLICE_MIN_EFFECTIVE == 400
    assert ew.FIRE_RATE_MIN == 0.05
    assert ew.MIN_RAW_LIFT_PTS == 5.0
    assert ew.ECON_MIN_NET == 0.10
    assert ew.ECON_ABSTAIN_NET == -0.10
    assert ew.BH_Q == 0.10
    assert ew.DSR_MIN == 0.95
    assert ew.MIN_CONFIRMATIONS == 3
    assert ew.CONFIRM_SPACING_TRADES == 100


# ── _num helper (defensive numeric coercion) ────────────────────────────────
def test_num_coerces_and_is_safe():
    assert ew._num(3) == 3.0
    assert ew._num("2.5") == 2.5
    assert ew._num(None) is None
    assert ew._num("not-a-number") is None
    assert ew._num([1, 2]) is None
    assert ew._num(float("nan")) is None
    assert ew._num(float("inf")) is None


# ── y_tp: 1 iff realized TP exit (single definition — spec I1) ──────────────
def test_y_tp_true_on_tp_exit():
    assert ew.y_tp({"exit_type": "TP"}) == 1


def test_y_tp_false_on_other_exits():
    assert ew.y_tp({"exit_type": "settle"}) == 0
    assert ew.y_tp({"exit_type": "stop"}) == 0
    assert ew.y_tp({"exit_type": None}) == 0
    assert ew.y_tp({}) == 0


def test_y_tp_does_not_blend_near_miss():
    # peak >= 18 is a NEAR-MISS counterfactual — must NOT count as a TP (spec I1).
    assert ew.y_tp({"exit_type": "settle", "peak_unrealized_pct": 25.0}) == 0


def test_y_tp_malformed_never_raises():
    assert ew.y_tp(None) == 0
    assert ew.y_tp("garbage") == 0
    assert ew.y_tp(123) == 0
    assert ew.y_tp([]) == 0


# ── y_dir: 1/0/None directional held-to-resolution, demo-fee-netted ─────────
def test_y_dir_win_when_held_pnl_positive():
    row = {
        "cf_exit_variants": {"pnl_if_held_to_resolution": 1.23},
        "resolved_outcome": "Up",
    }
    assert ew.y_dir(row) == 1


def test_y_dir_loss_when_held_pnl_nonpositive():
    row = {
        "cf_exit_variants": {"pnl_if_held_to_resolution": -0.5},
        "resolved_outcome": "Down",
    }
    assert ew.y_dir(row) == 0
    row0 = {
        "cf_exit_variants": {"pnl_if_held_to_resolution": 0.0},
        "resolved_outcome": "Up",
    }
    assert ew.y_dir(row0) == 0


def test_y_dir_none_on_void_or_non_resolved():
    # VOID / UNKNOWN -> resolved_outcome not in (Up, Down) -> None
    assert ew.y_dir(
        {"cf_exit_variants": {"pnl_if_held_to_resolution": 1.0}, "resolved_outcome": "VOID"}
    ) is None
    assert ew.y_dir(
        {"cf_exit_variants": {"pnl_if_held_to_resolution": 1.0}, "resolved_outcome": None}
    ) is None


def test_y_dir_none_when_counterfactual_missing():
    assert ew.y_dir({"resolved_outcome": "Up"}) is None
    assert ew.y_dir({"cf_exit_variants": {}, "resolved_outcome": "Up"}) is None
    assert ew.y_dir(
        {"cf_exit_variants": {"pnl_if_held_to_resolution": None}, "resolved_outcome": "Up"}
    ) is None


def test_y_dir_malformed_never_raises():
    assert ew.y_dir(None) is None
    assert ew.y_dir("garbage") is None
    assert ew.y_dir({"cf_exit_variants": "not-a-dict", "resolved_outcome": "Up"}) is None
    assert ew.y_dir(
        {"cf_exit_variants": {"pnl_if_held_to_resolution": "nan-string"}, "resolved_outcome": "Up"}
    ) is None


# ── r_net: stake-normalized, real-fee net $ (spec 3.1, fixes I3/I6) ──────────
def _wedge_real(fill, contracts):
    return (ew.REAL_RATE - 2 * ew.DEMO_FEE_RATE) * fill * contracts


def test_r_net_uses_real_notional_when_fill_and_contracts_present():
    row = {"realized_pnl": 0.90, "fill_price": 0.30, "contracts": 16.0}
    expected = (0.90 - _wedge_real(0.30, 16.0)) / 1.0
    assert ew.r_net(row) == expected


def test_r_net_falls_back_to_flat_stake_when_notional_missing():
    row = {"realized_pnl": 0.50}  # no fill_price / contracts
    expected = (0.50 - (ew.REAL_RATE - 2 * ew.DEMO_FEE_RATE) * ew.STAKE_USD) / 1.0
    assert ew.r_net(row) == expected


def test_r_net_fallback_when_fill_present_but_contracts_missing():
    row = {"realized_pnl": 0.50, "fill_price": 0.30}
    expected = (0.50 - (ew.REAL_RATE - 2 * ew.DEMO_FEE_RATE) * ew.STAKE_USD) / 1.0
    assert ew.r_net(row) == expected


def test_r_net_stake_normalizes_by_multiplier():
    # loss_recovery_multiplier = 4 -> divide net by 4
    row = {"realized_pnl": 2.0, "fill_price": 0.50, "contracts": 10.0,
           "loss_recovery_multiplier": 4.0}
    net = (2.0 - _wedge_real(0.50, 10.0))
    assert ew.r_net(row) == net / 4.0


def test_r_net_multiplier_floored_at_one():
    # a multiplier < 1 (or 0 / negative / None) must never amplify -> floor at 1.0
    base = {"realized_pnl": 1.0, "fill_price": 0.40, "contracts": 5.0}
    expected = (1.0 - _wedge_real(0.40, 5.0)) / 1.0
    assert ew.r_net({**base, "loss_recovery_multiplier": 0.5}) == expected
    assert ew.r_net({**base, "loss_recovery_multiplier": 0}) == expected
    assert ew.r_net({**base, "loss_recovery_multiplier": None}) == expected


def test_r_net_none_when_no_realized_pnl():
    assert ew.r_net({}) is None
    assert ew.r_net({"realized_pnl": None}) is None


def test_r_net_real_wedge_exceeds_flat_for_pricey_fills():
    # sanity: the real-notional wedge differs from the flat-stake wedge.
    pricey = {"realized_pnl": 0.0, "fill_price": 0.95, "contracts": 100.0}
    flat = {"realized_pnl": 0.0}
    assert ew.r_net(pricey) < ew.r_net(flat)  # bigger notional -> bigger wedge -> more negative


def test_r_net_malformed_never_raises():
    assert ew.r_net(None) is None
    assert ew.r_net("garbage") is None
    # realized_pnl present but unparseable -> safe None (never raises)
    assert ew.r_net({"realized_pnl": "oops"}) is None
    # malformed fill/contracts fall back to the flat-stake wedge, never raise
    row = {"realized_pnl": 1.0, "fill_price": "x", "contracts": "y"}
    expected = (1.0 - (ew.REAL_RATE - 2 * ew.DEMO_FEE_RATE) * ew.STAKE_USD) / 1.0
    assert ew.r_net(row) == expected


# ── clean: martingale / exploration confound filter (spec G5) ───────────────
def test_clean_passes_plain_row():
    row = {"rule_flags": {"recovery_active": False},
           "loss_recovery_multiplier": 1.0, "exploration_flag": 0}
    assert ew.clean(row) is True


def test_clean_passes_empty_row():
    # nothing set -> defaults are clean (recovery not active, mult 1.0, no exploration)
    assert ew.clean({}) is True


def test_clean_drops_recovery_active():
    assert ew.clean({"rule_flags": {"recovery_active": True}}) is False


def test_clean_drops_multiplier_above_one():
    assert ew.clean({"loss_recovery_multiplier": 2.0}) is False
    assert ew.clean({"loss_recovery_multiplier": 1.5}) is False


def test_clean_drops_exploration_rows():
    assert ew.clean({"exploration_flag": 1}) is False
    assert ew.clean({"exploration_flag": True}) is False


def test_clean_keeps_exploration_false_none_zero():
    assert ew.clean({"exploration_flag": 0}) is True
    assert ew.clean({"exploration_flag": False}) is True
    assert ew.clean({"exploration_flag": None}) is True


def test_clean_malformed_never_raises():
    assert ew.clean(None) is False
    assert ew.clean("garbage") is False
    # rule_flags not a dict -> safe (treated as no recovery)
    assert ew.clean({"rule_flags": "nope", "loss_recovery_multiplier": 1.0,
                     "exploration_flag": 0}) is True
    # unparseable multiplier -> treated as 1.0 (clean), never raises
    assert ew.clean({"loss_recovery_multiplier": "weird"}) is True


# ===========================================================================
# Task 3: FEATURES list + feature_value + walk-forward split + frozen
# bucketizer (tertile edges per fold TRAIN segment only — leak guard T8).
# ===========================================================================


def _row(ts, *, ta=None, clob=None, side="Up", vol_bucket="mid", window_sec=300):
    """A minimal light=False-shaped row with a decision_ts and nested context."""
    ctx = {"ta": {"features": dict(ta or {})}, "clob": dict(clob or {})}
    return {
        "decision_ts": ts,
        "context": ctx,
        "side": side,
        "vol_bucket": vol_bucket,
        "window_sec": window_sec,
    }


# ── FEATURES: the ~40 (path_fn, name, kind) entries from spec §3.1 ──────────
def test_features_list_shape_and_size():
    feats = ew.FEATURES
    assert isinstance(feats, (list, tuple))
    # spec §3.1 says ~40 features
    assert 35 <= len(feats) <= 50
    names = [f[1] for f in feats]
    assert len(names) == len(set(names))  # unique names
    for path_fn, name, kind in feats:
        assert callable(path_fn)
        assert isinstance(name, str) and name
        assert kind in ("cont", "cat")


def test_features_cover_ta_clob_and_categorical():
    names = {f[1] for f in ew.FEATURES}
    kinds = {f[1]: f[2] for f in ew.FEATURES}
    # representative continuous TA features
    for n in ("rsi7", "rsi30", "macd_pct", "bb_pct_b", "rv_5", "rv_15",
              "rv_30", "atr_pct", "ema9_21_ratio", "volume_z", "ret_1m"):
        assert n in names and kinds[n] == "cont"
    # representative CLOB microstructure features (per side)
    assert any("spread" in n for n in names)
    assert any("imbalance" in n for n in names)
    assert any("depth" in n or "microprice" in n for n in names)
    # representative categoricals kept as-is
    for n in ("side", "vol_bucket"):
        assert n in names and kinds[n] == "cat"


# ── feature_value: safe nested read -> float | str | None ───────────────────
def test_feature_value_reads_ta_feature():
    fmap = {f[1]: f[0] for f in ew.FEATURES}
    row = _row(1, ta={"rsi7": 71.5})
    assert fmap["rsi7"](row) == 71.5


def test_feature_value_reads_clob_feature():
    fmap = {f[1]: f[0] for f in ew.FEATURES}
    # find a CLOB spread feature and feed a matching nested value
    clob_feat = next(f for f in ew.FEATURES if "spread" in f[1])
    side = "up" if "up" in clob_feat[1] else "down"
    row = _row(1, clob={side: {"spread_pct": 1.25}})
    assert clob_feat[0](row) == 1.25


def test_feature_value_reads_categorical_as_is():
    fmap = {f[1]: f[0] for f in ew.FEATURES}
    assert fmap["side"](_row(1, side="Down")) == "Down"
    assert fmap["vol_bucket"](_row(1, vol_bucket="high")) == "high"


def test_feature_value_safe_on_missing_and_malformed():
    # every accessor tolerates missing/None/malformed nesting without raising
    for path_fn, name, kind in ew.FEATURES:
        assert path_fn({}) is None or isinstance(path_fn({}), (float, str))
        assert path_fn(None) is None
        assert path_fn({"context": "not-a-dict"}) is None
        assert path_fn({"context": {"ta": "x", "clob": "y"}}) is None


def test_feature_value_helper_direct():
    # the public helper, if exposed, mirrors the path_fn behaviour
    fmap = {f[1]: f[0] for f in ew.FEATURES}
    row = _row(1, ta={"macd_pct": -0.4})
    assert ew.feature_value(row, "macd_pct") == fmap["macd_pct"](row)
    assert ew.feature_value(row, "no_such_feature") is None
    assert ew.feature_value(None, "rsi7") is None


# ── _walk_forward_split: time-ordered 70/30, vault = most-recent, no shuffle ─
def test_walk_forward_split_70_30_by_time():
    rows = [_row(ts) for ts in range(100)]
    disc, vault = ew._walk_forward_split(rows)
    assert len(disc) == 70 and len(vault) == 30
    # discovery is the OLDER 70%, vault is the MOST-RECENT 30%
    assert [r["decision_ts"] for r in disc] == list(range(70))
    assert [r["decision_ts"] for r in vault] == list(range(70, 100))


def test_walk_forward_split_sorts_unordered_input_never_shuffles():
    import random as _random
    order = list(range(100))
    _random.Random(123).shuffle(order)
    rows = [_row(ts) for ts in order]
    disc, vault = ew._walk_forward_split(rows)
    # output is strictly time-ascending regardless of input order
    d_ts = [r["decision_ts"] for r in disc]
    v_ts = [r["decision_ts"] for r in vault]
    assert d_ts == sorted(d_ts)
    assert v_ts == sorted(v_ts)
    # every vault ts is strictly later than every discovery ts (time seal)
    assert max(d_ts) < min(v_ts)
    assert d_ts == list(range(70)) and v_ts == list(range(70, 100))


def test_walk_forward_split_safe_on_degenerate():
    assert ew._walk_forward_split([]) == ([], [])
    assert ew._walk_forward_split(None) == ([], [])
    d, v = ew._walk_forward_split([_row(5)])  # tiny input never raises
    assert isinstance(d, list) and isinstance(v, list)


# ── _folds: >=5 expanding-window folds with a 2-trade boundary embargo ──────
def test_folds_at_least_five_expanding():
    disc = [_row(ts) for ts in range(70)]
    folds = ew._folds(disc, k=5)
    assert len(folds) >= 5
    train_sizes = []
    for train, test in folds:
        assert isinstance(train, list) and isinstance(test, list)
        assert len(train) > 0 and len(test) > 0
        # every train ts precedes every test ts (forward-only)
        assert max(r["decision_ts"] for r in train) < min(r["decision_ts"] for r in test)
        train_sizes.append(len(train))
    # expanding window: train grows monotonically
    assert train_sizes == sorted(train_sizes)
    assert train_sizes[0] < train_sizes[-1]


def test_folds_embargo_two_boundary_trades():
    disc = [_row(ts) for ts in range(70)]
    folds = ew._folds(disc, k=5)
    # the 2 trades straddling each train/test boundary are embargoed: the last
    # train ts and the first test ts are separated by >= 2 dropped rows.
    for train, test in folds:
        last_train = max(r["decision_ts"] for r in train)
        first_test = min(r["decision_ts"] for r in test)
        assert first_test - last_train >= 3  # 2 embargoed rows in the gap


def test_folds_safe_on_degenerate():
    assert ew._folds([], k=5) == []
    assert ew._folds(None, k=5) == []
    assert isinstance(ew._folds([_row(1), _row(2)], k=5), list)  # too small -> no raise


# ── bucketize: tertile edges from the TRAIN SEGMENT ONLY (spec I5) ───────────
def test_bucketize_continuous_tertiles_label():
    feat = next(f for f in ew.FEATURES if f[2] == "cont" and f[1] == "rsi7")
    train = [_row(i, ta={"rsi7": float(i)}) for i in range(99)]  # 0..98
    b = ew.bucketize(train, feat)
    # low / mid / high tertile labels by value — three distinct frozen buckets
    lo_label = b(_row(0, ta={"rsi7": 0.0}))    # bottom tertile
    hi_label = b(_row(0, ta={"rsi7": 98.0}))   # top tertile
    mid_label = b(_row(0, ta={"rsi7": 49.0}))  # middle tertile
    assert lo_label.endswith("low") and hi_label.endswith("high") and mid_label.endswith("mid")
    assert lo_label != hi_label and lo_label != mid_label and mid_label != hi_label
    # a missing feature value -> a safe non-crashing bucket (None)
    assert b(_row(0)) is None or isinstance(b(_row(0)), str)


def test_bucketize_categorical_as_is():
    feat = next(f for f in ew.FEATURES if f[1] == "side")
    train = [_row(i, side="Up") for i in range(10)] + [_row(i, side="Down") for i in range(10)]
    b = ew.bucketize(train, feat)
    assert b(_row(0, side="Up")) != b(_row(0, side="Down"))
    # value present -> stable label keyed on the category
    assert b(_row(0, side="Up")) == b(_row(99, side="Up"))


def test_bucketize_frozen_on_train_segment_leak_guard_T8():
    """T8 — tertile edges computed on a fold's TRAIN segment do NOT change when
    test data is appended. Construct a case where refitting on the full set would
    move a boundary, and assert the frozen edge ignores the appended test rows."""
    feat = next(f for f in ew.FEATURES if f[1] == "rsi7")
    # TRAIN: values 0..9 (tertile cut-points ~3 and ~6)
    train = [_row(i, ta={"rsi7": float(i)}) for i in range(10)]
    frozen = ew.bucketize(train, feat)
    # A probe value that sits in the TRAIN-based MID tertile.
    probe = _row(999, ta={"rsi7": 5.0})
    label_train_only = frozen(probe)

    # Now imagine refitting on train + a flood of huge test values: that would
    # push the cut-points way up, moving 5.0 into the BOTTOM tertile.
    full = train + [_row(100 + i, ta={"rsi7": 1000.0 + i}) for i in range(40)]
    refit = ew.bucketize(full, feat)
    label_refit = refit(probe)

    # The frozen (train-only) edge must be UNAFFECTED by the appended test data:
    # the value did not change, only the cut-points the *refit* used.
    assert frozen(probe) == label_train_only  # still frozen
    # And refitting genuinely moves the boundary (otherwise the test is vacuous).
    assert label_refit != label_train_only


def test_bucketize_safe_on_degenerate():
    feat = next(f for f in ew.FEATURES if f[1] == "rsi7")
    b = ew.bucketize([], feat)        # empty train -> callable, never raises
    assert callable(b)
    assert b(_row(0, ta={"rsi7": 1.0})) is None or isinstance(b(_row(0, ta={"rsi7": 1.0})), str)
    b2 = ew.bucketize(None, feat)
    assert callable(b2)
