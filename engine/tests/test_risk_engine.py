"""
Tests for engine/risk_engine.py — the MANDATORY risk layer between any signal
and any order. Bare imports (engine/ on path via conftest.py).

Run: python3 -m pytest engine/tests/test_risk_engine.py -q

Covered (per spec):
  * fixed-fractional sizing math (equity 1000, entry 100, stop 98, 0.5% -> qty 2.5)
  * R:R < 2 -> reject (size 0 + reason)
  * effective leverage > leverage_cap -> reject
  * risk_pct > max_risk_pct -> reject
  * check_caps: daily -3% -> flatten + block new; global -10% -> halt
  * gate_order is the ONLY approve path (bot AND manual)
  * HARD invariants: never increases size after a loss, never widens a stop,
    no martingale multiplier — asserted across a loss streak.
  * never raises -> safe reject on any malformed input.
"""
from __future__ import annotations

import risk_engine as R
from risk_engine import check_caps, gate_order, size_position


# ---------------------------------------------------------------------------
# size_position — fixed-fractional sizing math
# ---------------------------------------------------------------------------

def test_sizing_basic_math():
    # equity 1000, entry 100, stop 98 -> risk-per-unit 2; 0.5% of 1000 = $5 risk
    # qty = 5 / 2 = 2.5
    res = size_position(1000.0, 100.0, 98.0, risk_pct=0.5)
    assert res["qty"] == 2.5
    assert res["approved"] is True
    assert res["risk_dollars"] == 5.0
    assert res["risk_per_unit"] == 2.0


def test_sizing_short_uses_abs_distance():
    # short: entry 100, stop 102 -> distance 2, same math
    res = size_position(1000.0, 100.0, 102.0, risk_pct=0.5)
    assert res["qty"] == 2.5
    assert res["approved"] is True


def test_sizing_scales_linearly_with_equity():
    small = size_position(1000.0, 100.0, 98.0, risk_pct=0.5)
    big = size_position(2000.0, 100.0, 98.0, risk_pct=0.5)
    # Double equity -> double size (pure fixed-fractional, no martingale).
    assert big["qty"] == 2 * small["qty"]


# ---------------------------------------------------------------------------
# size_position — rejections
# ---------------------------------------------------------------------------

def test_reject_risk_pct_over_max():
    res = size_position(1000.0, 100.0, 98.0, risk_pct=2.5, max_risk_pct=2.0)
    assert res["approved"] is False
    assert res["qty"] == 0
    assert "risk_pct" in res["reason"].lower() or "max" in res["reason"].lower()


def test_reject_leverage_over_cap():
    # Tight stop -> huge qty -> notional/equity > leverage_cap.
    # entry 100, stop 99.9 -> distance 0.1; 0.5% of 1000 = $5 -> qty 50 ->
    # notional 5000 -> 5x leverage > 3x cap.
    res = size_position(1000.0, 100.0, 99.9, risk_pct=0.5, leverage_cap=3.0)
    assert res["approved"] is False
    assert res["qty"] == 0
    assert "leverage" in res["reason"].lower()


def test_reject_rr_below_2_when_target_given():
    # R:R is checked when a target is supplied. entry 100, stop 98 (risk 2),
    # target 103 -> reward 3 -> R:R 1.5 < 2 -> reject.
    res = size_position(1000.0, 100.0, 98.0, risk_pct=0.5, target=103.0, min_rr=2.0)
    assert res["approved"] is False
    assert res["qty"] == 0
    assert "r:r" in res["reason"].lower() or "reward" in res["reason"].lower()


def test_accept_rr_at_or_above_2():
    res = size_position(1000.0, 100.0, 98.0, risk_pct=0.5, target=104.0, min_rr=2.0)
    assert res["approved"] is True
    assert res["qty"] == 2.5


def test_reject_zero_stop_distance():
    res = size_position(1000.0, 100.0, 100.0, risk_pct=0.5)
    assert res["approved"] is False
    assert res["qty"] == 0


def test_reject_nonpositive_equity():
    res = size_position(0.0, 100.0, 98.0, risk_pct=0.5)
    assert res["approved"] is False
    assert res["qty"] == 0


def test_never_raises_on_garbage():
    for args in (
        (None, None, None),
        ("x", "y", "z"),
        (float("nan"), 100.0, 98.0),
        (1000.0, float("inf"), 98.0),
        (1000.0, 100.0, float("nan")),
    ):
        res = size_position(*args)
        assert res["approved"] is False
        assert res["qty"] == 0
        assert isinstance(res["reason"], str) and res["reason"]


# ---------------------------------------------------------------------------
# check_caps — daily + global loss caps
# ---------------------------------------------------------------------------

def test_caps_normal_allows_new():
    res = check_caps(-1.0, -2.0)
    assert res["allow_new"] is True
    assert res["flatten"] is False
    assert res["halt"] is False


def test_caps_daily_stop_flattens_and_blocks():
    res = check_caps(-3.0, -3.0, daily_stop=-3.0)
    assert res["allow_new"] is False
    assert res["flatten"] is True
    assert "daily" in res["reason"].lower()


def test_caps_daily_stop_breached_worse():
    res = check_caps(-5.0, -5.0, daily_stop=-3.0)
    assert res["allow_new"] is False
    assert res["flatten"] is True


def test_caps_global_stop_halts():
    res = check_caps(-4.0, -10.0, global_stop=-10.0)
    assert res["halt"] is True
    assert res["allow_new"] is False
    assert res["flatten"] is True
    assert "global" in res["reason"].lower()


def test_caps_never_raises_on_garbage():
    res = check_caps(None, "x")
    # Unknown PnL must fail safe (block), not crash.
    assert res["allow_new"] is False
    assert isinstance(res["reason"], str) and res["reason"]


# ---------------------------------------------------------------------------
# gate_order — the ONLY approve path
# ---------------------------------------------------------------------------

def _good_signal():
    return {"signal": "long", "entry": 100.0, "stop": 98.0, "target": 104.0, "rr": 2.0}


def _ok_risk_state():
    return {"day_pnl_pct": -0.5, "peak_drawdown_pct": -1.0}


def test_gate_order_approves_good_setup():
    res = gate_order(_good_signal(), 1000.0, _ok_risk_state(), {})
    assert res["approved"] is True
    assert res["qty"] == 2.5
    assert res["reason"]


def test_gate_order_blocks_when_daily_cap_hit():
    rs = {"day_pnl_pct": -3.0, "peak_drawdown_pct": -3.0}
    res = gate_order(_good_signal(), 1000.0, rs, {})
    assert res["approved"] is False
    assert res["qty"] == 0
    assert "daily" in res["reason"].lower()


def test_gate_order_blocks_when_halted():
    rs = {"day_pnl_pct": -4.0, "peak_drawdown_pct": -10.0}
    res = gate_order(_good_signal(), 1000.0, rs, {})
    assert res["approved"] is False
    assert res["qty"] == 0
    assert "halt" in res["reason"].lower() or "global" in res["reason"].lower()


def test_gate_order_rejects_flat_signal():
    flat = {"signal": "flat", "entry": None, "stop": None, "target": None}
    res = gate_order(flat, 1000.0, _ok_risk_state(), {})
    assert res["approved"] is False
    assert res["qty"] == 0


def test_gate_order_rejects_bad_rr():
    sig = {"signal": "long", "entry": 100.0, "stop": 98.0, "target": 103.0}
    res = gate_order(sig, 1000.0, _ok_risk_state(), {})
    assert res["approved"] is False
    assert res["qty"] == 0


def test_gate_order_respects_config_risk_pct():
    # risk_pct override via config: 1% of 1000 = $10 / $2 = qty 5
    res = gate_order(_good_signal(), 1000.0, _ok_risk_state(), {"risk_pct": 1.0})
    assert res["approved"] is True
    assert res["qty"] == 5.0


def test_gate_order_rejects_risk_pct_over_max():
    res = gate_order(_good_signal(), 1000.0, _ok_risk_state(),
                     {"risk_pct": 5.0, "max_risk_pct": 2.0})
    assert res["approved"] is False
    assert res["qty"] == 0


def test_gate_order_never_raises():
    for sig in (None, {}, {"signal": "long"}, "x", 42):
        res = gate_order(sig, 1000.0, _ok_risk_state(), {})
        assert res["approved"] is False
        assert res["qty"] == 0
        assert isinstance(res["reason"], str) and res["reason"]


# ---------------------------------------------------------------------------
# HARD invariants — NO MARTINGALE (the whole point)
# ---------------------------------------------------------------------------

def test_loss_streak_does_not_grow_size():
    """A sequence of losses must NEVER increase position size.

    We simulate a losing streak by shrinking equity each loss and confirm size
    monotonically decreases (fixed-fractional) — the opposite of martingale.
    """
    equity = 1000.0
    signal = _good_signal()
    last_qty = None
    for _ in range(6):
        rs = {"day_pnl_pct": -0.1, "peak_drawdown_pct": -0.1}
        res = gate_order(signal, equity, rs, {})
        assert res["approved"] is True
        qty = res["qty"]
        if last_qty is not None:
            # After a loss, size must be <= the previous size. NEVER larger.
            assert qty <= last_qty, f"size grew after loss: {last_qty} -> {qty}"
        last_qty = qty
        # take the loss: equity drops by the risked amount
        equity -= res["risk_dollars"]


def test_same_equity_same_size_no_streak_memory():
    """The engine is stateless w.r.t. prior outcomes: identical inputs -> identical
    size, regardless of how many losses preceded. No hidden martingale counter."""
    sig = _good_signal()
    a = gate_order(sig, 1000.0, _ok_risk_state(), {})
    b = gate_order(sig, 1000.0, _ok_risk_state(), {})
    assert a["qty"] == b["qty"]


def test_never_widens_stop():
    """gate_order must use the signal's stop verbatim — never move it further
    from entry (widening a stop is the cousin of martingale)."""
    sig = _good_signal()
    res = gate_order(sig, 1000.0, _ok_risk_state(), {})
    assert res["approved"] is True
    # The stop the engine sized against is exactly the signal's stop.
    assert res["stop"] == sig["stop"]


def test_no_martingale_symbols_in_module():
    forbidden = ("double", "multiplier", "size_up", "add_to", "average_down",
                 "martingale", "recover_loss", "increase_after_loss")
    public = [name for name in dir(R) if not name.startswith("_")]
    for name in public:
        low = name.lower()
        assert all(f not in low for f in forbidden), f"suspicious symbol: {name}"


def test_size_independent_of_loss_history_argument():
    """Even if a caller tries to pass prior-loss context, size must not grow.

    The engine ignores any 'loss_streak'/'last_loss' hints — there is no input
    that can make it size up. Passing such hints changes nothing.
    """
    sig = _good_signal()
    plain = gate_order(sig, 1000.0, _ok_risk_state(), {})
    tampered = gate_order(sig, 1000.0,
                          {"day_pnl_pct": -0.5, "peak_drawdown_pct": -1.0,
                           "loss_streak": 5, "last_loss": True},
                          {"recover": True, "double_after_loss": True})
    assert tampered["qty"] == plain["qty"]
    assert tampered["qty"] <= plain["qty"]
