"""טסטים לשחזור אחרי הפסד (מכפיל השקעה)."""
from __future__ import annotations

import pytest

from demo_engine import DemoState
from loss_recovery import apply_loss_recovery_from_settlements


def _state() -> DemoState:
    return DemoState(
        balance_usd=1000.0,
        loss_recovery_streak=0,
        loss_recovery_multiplier=1.0,
    )


def test_win_resets_streak_and_multiplier():
    st = _state()
    st.loss_recovery_streak = 5
    st.loss_recovery_multiplier = 2.5
    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=20.0,
        every_n_losses=1,
        max_multiplier=10.0,
        settlement_trades=[{"realized_pnl": 10.0}],
    )
    assert st.loss_recovery_streak == 0
    assert st.loss_recovery_multiplier == 1.0


def test_loss_increases_multiplier_every_n():
    st = _state()
    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=20.0,
        every_n_losses=1,
        max_multiplier=10.0,
        settlement_trades=[{"realized_pnl": -1.0}],
    )
    assert st.loss_recovery_streak == 1
    assert st.loss_recovery_multiplier == pytest.approx(1.2)

    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=20.0,
        every_n_losses=1,
        max_multiplier=10.0,
        settlement_trades=[{"realized_pnl": -1.0}],
    )
    assert st.loss_recovery_streak == 2
    assert st.loss_recovery_multiplier == pytest.approx(1.44)


def test_every_three_losses_only():
    st = _state()
    for _ in range(2):
        apply_loss_recovery_from_settlements(
            st,
            enabled=True,
            step_pct=50.0,
            every_n_losses=3,
            max_multiplier=100.0,
            settlement_trades=[{"realized_pnl": -1.0}],
        )
    assert st.loss_recovery_streak == 2
    assert st.loss_recovery_multiplier == 1.0

    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=50.0,
        every_n_losses=3,
        max_multiplier=100.0,
        settlement_trades=[{"realized_pnl": -1.0}],
    )
    assert st.loss_recovery_streak == 3
    assert st.loss_recovery_multiplier == pytest.approx(1.5)


def test_max_multiplier_cap():
    st = _state()
    st.loss_recovery_multiplier = 9.0
    st.loss_recovery_streak = 0
    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=50.0,
        every_n_losses=1,
        max_multiplier=10.0,
        settlement_trades=[{"realized_pnl": -1.0}],
    )
    assert st.loss_recovery_multiplier == 10.0


def test_disabled_no_op():
    st = _state()
    apply_loss_recovery_from_settlements(
        st,
        enabled=False,
        step_pct=20.0,
        every_n_losses=1,
        max_multiplier=10.0,
        settlement_trades=[{"realized_pnl": -5.0}],
    )
    assert st.loss_recovery_streak == 0
    assert st.loss_recovery_multiplier == 1.0


def test_zero_step_pct_no_bump():
    st = _state()
    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=0.0,
        every_n_losses=1,
        max_multiplier=10.0,
        settlement_trades=[{"realized_pnl": -1.0}],
    )
    assert st.loss_recovery_streak == 1
    assert st.loss_recovery_multiplier == 1.0


def test_returns_log_lines_for_settlement():
    st = _state()
    lines = apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=20.0,
        every_n_losses=1,
        max_multiplier=10.0,
        settlement_trades=[
            {"realized_pnl": -5.0, "type": "SETTLE_LOSS", "session_id": "abc12345"},
        ],
    )
    assert len(lines) == 1
    assert "שחזור הפסד" in lines[0]
    assert "SETTLE_LOSS" in lines[0]
    assert "מכפיל" in lines[0]
