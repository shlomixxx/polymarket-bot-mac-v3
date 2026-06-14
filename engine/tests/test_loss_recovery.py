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
    """ה-config מתיר 10×, אבל תקרת-הברזל (3.0) חוסמת את הצבירה — incident 2026-06-15."""
    from strategy_runner import HARD_MAX_LOSS_RECOVERY_MULT

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
    # נחסם בתקרת-הברזל, לא ב-cap של ה-config (10) ולא בכפל 9×1.5
    assert st.loss_recovery_multiplier == HARD_MAX_LOSS_RECOVERY_MULT == 3.0


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


def test_settle_unknown_does_not_escalate_multiplier():
    """תקלת ה-incident: תוצאה לא-ידועה (כשל מחיר BTC) נספרה כהפסד והסלימה את המכפיל.
    כעת SETTLE_UNKNOWN חייב להישאר ניטרלי — לא מסלים ולא מאפס."""
    st = _state()
    st.loss_recovery_streak = 3
    st.loss_recovery_multiplier = 6.25
    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=150.0,
        every_n_losses=1,
        max_multiplier=10000.0,
        settlement_trades=[
            {"realized_pnl": -127.44, "type": "SETTLE_UNKNOWN", "settlement_error": "btc_prices_unavailable"},
        ],
    )
    # ללא שינוי — לא מסלים על תוצאה לא-ידועה
    assert st.loss_recovery_streak == 3
    assert st.loss_recovery_multiplier == pytest.approx(6.25)


def test_settlement_error_flag_is_skipped():
    st = _state()
    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=150.0,
        every_n_losses=1,
        max_multiplier=10000.0,
        settlement_trades=[{"realized_pnl": -50.0, "settlement_error": "missing_window_epoch"}],
    )
    assert st.loss_recovery_streak == 0
    assert st.loss_recovery_multiplier == 1.0


def test_incident_replay_unknowns_do_not_explode_multiplier():
    """10 תוצאות SETTLE_UNKNOWN רצופות (כמו ב-incident) — המכפיל נשאר 1.0, לא 9537."""
    st = _state()
    for _ in range(10):
        apply_loss_recovery_from_settlements(
            st,
            enabled=True,
            step_pct=150.0,
            every_n_losses=1,
            max_multiplier=10000.0,
            settlement_trades=[{"realized_pnl": -100.0, "type": "SETTLE_UNKNOWN", "settlement_error": "btc_prices_unavailable"}],
        )
    assert st.loss_recovery_multiplier == 1.0
    assert st.loss_recovery_streak == 0


def test_real_loss_still_escalates_after_fix():
    """ודא שהתיקון לא שבר את ההתנהגות הרצויה: הפסד אמיתי (SETTLE_LOSS) עדיין מסלים."""
    st = _state()
    apply_loss_recovery_from_settlements(
        st,
        enabled=True,
        step_pct=150.0,
        every_n_losses=1,
        max_multiplier=10000.0,
        settlement_trades=[{"realized_pnl": -10.0, "type": "SETTLE_LOSS"}],
    )
    assert st.loss_recovery_streak == 1
    assert st.loss_recovery_multiplier == pytest.approx(2.5)


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
