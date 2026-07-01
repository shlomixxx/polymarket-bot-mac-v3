"""
Tests for the Chop-Armed Follow-the-Winner strategy.

The bot waits for a "chop" of N strictly-alternating 5-min windows (🔴🟢🔴🟢), then
runs a Follow-Last-Winner campaign with bounded martingale (reuses loss_recovery),
and ends the campaign when a loss happens at the max multiplier — back to waiting.

Pure logic lives in chop_gate.py (I/O-free, never raises); the wiring lives on
StrategyRunner (gate in _entry_limits_ok, campaign transitions on settlement).
"""
import chop_gate as cg


# ── Pure: is_chop (sides are MOST-RECENT-FIRST, as get_last_window_winners returns) ──

def test_is_chop_true_on_strict_alternation():
    assert cg.is_chop(["Up", "Down", "Up", "Down"], 4) is True
    assert cg.is_chop(["Down", "Up", "Down", "Up", "Down"], 5) is True


def test_is_chop_false_on_repeat():
    assert cg.is_chop(["Up", "Up", "Down", "Up"], 4) is False
    assert cg.is_chop(["Up", "Down", "Down", "Up"], 4) is False


def test_is_chop_false_when_too_short():
    assert cg.is_chop(["Up", "Down", "Up"], 4) is False
    assert cg.is_chop([], 4) is False


def test_is_chop_ignores_windows_beyond_n():
    # 6 alternating, n=4 → only the first 4 matter → True even though [4],[5] continue.
    assert cg.is_chop(["Up", "Down", "Up", "Down", "Up", "Down"], 4) is True
    # first 4 alternate, extra tail broken → still True (tail ignored)
    assert cg.is_chop(["Up", "Down", "Up", "Down", "Down"], 4) is True


def test_is_chop_false_on_none_or_bad_side():
    assert cg.is_chop(["Up", None, "Up", "Down"], 4) is False
    assert cg.is_chop(["Up", "", "Up", "Down"], 4) is False
    assert cg.is_chop(["Up", "sideways", "Up", "Down"], 4) is False


def test_is_chop_requires_n_at_least_2():
    # n<2 is not a meaningful chop
    assert cg.is_chop(["Up"], 1) is False
    assert cg.is_chop(["Up", "Down"], 1) is False


def test_is_chop_never_raises_on_garbage():
    assert cg.is_chop(None, 4) is False          # type: ignore[arg-type]
    assert cg.is_chop("nope", 4) is False          # type: ignore[arg-type]
    assert cg.is_chop(["Up", "Down"], None) is False  # type: ignore[arg-type]


# ── Pure: campaign_should_end ────────────────────────────────────────────────

def test_campaign_should_end_loss_at_cap():
    assert cg.campaign_should_end(multiplier=3.0, cap=3.0, had_loss=True) is True
    assert cg.campaign_should_end(multiplier=3.5, cap=3.0, had_loss=True) is True  # >= cap


def test_campaign_should_end_loss_below_cap_is_false():
    assert cg.campaign_should_end(multiplier=1.0, cap=3.0, had_loss=True) is False
    assert cg.campaign_should_end(multiplier=2.0, cap=3.0, had_loss=True) is False


def test_campaign_should_end_win_never_ends():
    assert cg.campaign_should_end(multiplier=3.0, cap=3.0, had_loss=False) is False
    assert cg.campaign_should_end(multiplier=99.0, cap=3.0, had_loss=False) is False


def test_campaign_should_end_cap1_any_loss_ends():
    # max multiplier 1× (no doubling) → any loss ends the campaign at once
    assert cg.campaign_should_end(multiplier=1.0, cap=1.0, had_loss=True) is True


def test_campaign_should_end_never_raises_on_garbage():
    assert cg.campaign_should_end(multiplier=None, cap=None, had_loss=True) is False  # type: ignore[arg-type]
    assert cg.campaign_should_end(multiplier="x", cap="y", had_loss=True) is False  # type: ignore[arg-type]


# ── Wiring: gate + campaign on the real StrategyRunner ──────────────────────────
import os
import tempfile
import time
from pathlib import Path

from strategy_runner import StrategyConfig, StrategyRunner


def _fresh_tracker(tmp_dir: Path):
    """Reload history_tracker against a clean DATA_ROOT so each test has an isolated DB."""
    os.environ["DATA_ROOT"] = str(tmp_dir)
    import importlib
    import history_tracker
    importlib.reload(history_tracker)
    return history_tracker


def _seed(ht, sides_chrono, window_sec=300, base_epoch=1_000_000):
    """Record windows in CHRONOLOGICAL order (oldest first) with clear drift."""
    for i, s in enumerate(sides_chrono):
        ep = base_epoch + i * window_sec
        close_p = 100_100.0 if s == "Up" else 99_900.0
        ht.record_window_result(epoch=ep, slug=f"s{ep}", side_won=s,
                                btc_open=100_000.0, btc_close=close_p, window_sec=window_sec)


def _make_runner(tmp_dir: Path, cfg: StrategyConfig) -> StrategyRunner:
    from demo_engine import DemoEngine, DemoState
    eng = DemoEngine(state_path=tmp_dir / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    r = StrategyRunner(eng)
    r.rt.config = cfg
    return r


def test_gate_off_is_noop():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        _seed(ht, ["Up", "Up", "Up", "Up"])  # not a chop, but feature is off → entries allowed
        r = _make_runner(Path(d), StrategyConfig(chop_armed_flw_enabled=False))
        assert r._entry_limits_ok(now=time.time(), cfg=r.rt.config, planned_cost_usd=1.0) is True
        assert r.rt.chop_campaign_active is False


def test_gate_waiting_blocks_without_chop():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        _seed(ht, ["Up", "Up", "Down", "Up"])  # not strictly alternating
        r = _make_runner(Path(d), StrategyConfig(chop_armed_flw_enabled=True, chop_length_n=4))
        assert r._entry_limits_ok(now=time.time(), cfg=r.rt.config, planned_cost_usd=1.0) is False
        assert r.rt.chop_campaign_active is False
        assert r.rt.chop_campaign_state == "waiting"


def test_gate_arms_on_chop_and_follows_last_winner():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        # chronological: Down,Up,Down,Up → most-recent-first Up,Down,Up,Down (chop of 4), last winner Up
        _seed(ht, ["Down", "Up", "Down", "Up"])
        r = _make_runner(Path(d), StrategyConfig(chop_armed_flw_enabled=True, chop_length_n=4))
        assert r._entry_limits_ok(now=time.time(), cfg=r.rt.config, planned_cost_usd=1.0) is True
        assert r.rt.chop_campaign_active is True
        assert r.rt.chop_campaign_state == "armed"
        assert r.rt.chop_campaign_direction == "Up"  # follow the last winner


def test_gate_active_campaign_allows_entry_even_without_fresh_chop():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        _seed(ht, ["Up", "Up", "Up", "Up"])  # NOT a chop now
        r = _make_runner(Path(d), StrategyConfig(chop_armed_flw_enabled=True, chop_length_n=4))
        r.rt.chop_campaign_active = True  # a campaign is already running
        assert r._entry_limits_ok(now=time.time(), cfg=r.rt.config, planned_cost_usd=1.0) is True


def test_gate_needs_chop_length_min_2():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        _seed(ht, ["Down", "Up"])
        r = _make_runner(Path(d), StrategyConfig(chop_armed_flw_enabled=True, chop_length_n=2))
        assert r._entry_limits_ok(now=time.time(), cfg=r.rt.config, planned_cost_usd=1.0) is True
        assert r.rt.chop_campaign_direction == "Up"


# ── campaign transitions (drive _update_chop_campaign directly) ─────────────────

def _armed_runner(tmp_dir: Path, *, max_mult: float) -> StrategyRunner:
    r = _make_runner(tmp_dir, StrategyConfig(
        chop_armed_flw_enabled=True, chop_length_n=4,
        loss_recovery_enabled=True, loss_recovery_max_multiplier=max_mult,
    ))
    r.rt.chop_campaign_active = True
    r.rt.chop_campaign_state = "armed"
    r.rt.chop_campaign_direction = "Up"
    return r


def test_campaign_escalates_on_loss_below_cap_stays_active():
    with tempfile.TemporaryDirectory() as d:
        _fresh_tracker(Path(d))
        r = _armed_runner(Path(d), max_mult=3.0)
        r._update_chop_campaign(cfg=r.rt.config, had_loss=True, multiplier=2.0)  # below cap
        assert r.rt.chop_campaign_active is True


def test_campaign_resets_on_win_stays_active():
    with tempfile.TemporaryDirectory() as d:
        _fresh_tracker(Path(d))
        r = _armed_runner(Path(d), max_mult=3.0)
        r._update_chop_campaign(cfg=r.rt.config, had_loss=False, multiplier=1.0)  # a win
        assert r.rt.chop_campaign_active is True


def test_campaign_ends_on_loss_at_cap():
    with tempfile.TemporaryDirectory() as d:
        _fresh_tracker(Path(d))
        r = _armed_runner(Path(d), max_mult=3.0)
        r._update_chop_campaign(cfg=r.rt.config, had_loss=True, multiplier=3.0)  # loss at cap
        assert r.rt.chop_campaign_active is False
        assert r.rt.chop_campaign_state == "waiting"
        assert r.rt.chop_campaign_direction is None


def test_campaign_ends_immediately_when_cap_is_1():
    with tempfile.TemporaryDirectory() as d:
        _fresh_tracker(Path(d))
        r = _armed_runner(Path(d), max_mult=1.0)  # no doubling
        r._update_chop_campaign(cfg=r.rt.config, had_loss=True, multiplier=1.0)
        assert r.rt.chop_campaign_active is False


def test_rearm_on_next_chop_after_end():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        r = _armed_runner(Path(d), max_mult=3.0)
        # end the campaign
        r._update_chop_campaign(cfg=r.rt.config, had_loss=True, multiplier=3.0)
        assert r.rt.chop_campaign_active is False
        # a fresh chop appears → gate re-arms
        _seed(ht, ["Down", "Up", "Down", "Up"])
        assert r._entry_limits_ok(now=time.time(), cfg=r.rt.config, planned_cost_usd=1.0) is True
        assert r.rt.chop_campaign_active is True
        assert r.rt.chop_campaign_direction == "Up"
