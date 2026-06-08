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
