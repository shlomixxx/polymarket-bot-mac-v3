"""Loss-exit classification tests.

A non-settlement SELL exit that ends in a LOSS (floor-stop, or a peak-watchdog
retreat that dips below entry) must be recorded honestly as type="SELL_STOP"
(audit exit_type="stop") — NOT as a take-profit. A profitable exit stays
"SELL_TP"/exit_type="TP". This keeps the Trade Coach's exit-discipline lesson and
the circuit-breaker's consecutive-loss counter faithful.

These exercise the real DemoEngine.record_live_sell path (no network), which sets
the trade `type` from the realized PnL and finalizes the audit row.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from demo_engine import DemoEngine, DemoState, Position


def _apply_post_exit_streak(state, exit_trade: dict) -> None:
    """Verbatim copy of the FIX-3 post-exit streak gate in strategy_runner.tick()
    (the cfg.loss_recovery_enabled branch). A profitable exit resets the streak;
    a loss-exit builds it (a loss is a loss, not a win)."""
    _exit_rp = (exit_trade or {}).get("realized_pnl")
    if _exit_rp is not None and float(_exit_rp) >= 0:
        state.loss_recovery_streak = 0
        state.loss_recovery_multiplier = 1.0
    else:
        state.loss_recovery_streak += 1


def _engine_with_position(tmp_path: Path, *, avg_cost: float) -> tuple[DemoEngine, str]:
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    tok = "tok-exit"
    eng.state.positions.append(
        Position(side="Up", contracts=10.0, avg_cost=avg_cost, token_id=tok)
    )
    return eng, tok


@pytest.mark.asyncio
async def test_loss_exit_is_recorded_as_sell_stop(tmp_path: Path):
    """avg_cost 0.50, sold at 0.10 → realized < 0 → type == SELL_STOP."""
    eng, tok = _engine_with_position(tmp_path, avg_cost=0.50)
    r = await eng.record_live_sell(tok, 0.10)
    assert r["ok"] is True
    trade = r["trade"]
    assert float(trade["realized_pnl"]) < 0
    assert trade["type"] == "SELL_STOP"


@pytest.mark.asyncio
async def test_profitable_exit_stays_sell_tp(tmp_path: Path):
    """avg_cost 0.50, sold at 0.90 → realized >= 0 → type stays SELL_TP (unchanged)."""
    eng, tok = _engine_with_position(tmp_path, avg_cost=0.50)
    r = await eng.record_live_sell(tok, 0.90)
    assert r["ok"] is True
    trade = r["trade"]
    assert float(trade["realized_pnl"]) > 0
    assert trade["type"] == "SELL_TP"


@pytest.mark.asyncio
async def test_loss_exit_finalizes_audit_exit_type_stop(tmp_path: Path):
    """A losing full-exit finalizes the audit row with exit_type='stop' (not 'TP')."""
    eng, tok = _engine_with_position(tmp_path, avg_cost=0.50)
    eng._session_by_token[tok] = "sess-loss"
    captured: dict = {}

    def _fake_finalize(session_id, payload):
        captured["session_id"] = session_id
        captured["payload"] = payload
        return True

    import audit_tracker
    with patch.object(audit_tracker, "finalize_row", _fake_finalize):
        r = await eng.record_live_sell(tok, 0.10)

    assert r["ok"] is True
    assert captured["session_id"] == "sess-loss"
    assert captured["payload"]["type"] == "SELL_STOP"
    assert captured["payload"]["exit_type"] == "stop"


@pytest.mark.asyncio
async def test_profit_exit_finalizes_audit_exit_type_tp(tmp_path: Path):
    """A profitable full-exit still finalizes the audit row with exit_type='TP'."""
    eng, tok = _engine_with_position(tmp_path, avg_cost=0.50)
    eng._session_by_token[tok] = "sess-win"
    captured: dict = {}

    def _fake_finalize(session_id, payload):
        captured["payload"] = payload
        return True

    import audit_tracker
    with patch.object(audit_tracker, "finalize_row", _fake_finalize):
        r = await eng.record_live_sell(tok, 0.90)

    assert r["ok"] is True
    assert captured["payload"]["type"] == "SELL_TP"
    assert captured["payload"]["exit_type"] == "TP"


# ── FIX 3: post-exit loss-recovery streak gating (verbatim of the tick() branch) ──


def test_loss_exit_does_not_reset_streak():
    """A loss-exit (realized_pnl < 0) must NOT zero the streak — it builds it."""
    st = DemoState(balance_usd=1000.0, loss_recovery_streak=3, loss_recovery_multiplier=2.5)
    _apply_post_exit_streak(st, {"realized_pnl": -4.2})
    assert st.loss_recovery_streak == 4
    # multiplier left untouched here (escalation handled by the loss-recovery module)
    assert st.loss_recovery_multiplier == pytest.approx(2.5)


def test_profit_exit_resets_streak_and_multiplier():
    """A profitable exit (realized_pnl >= 0) resets the streak and multiplier (unchanged behavior)."""
    st = DemoState(balance_usd=1000.0, loss_recovery_streak=3, loss_recovery_multiplier=2.5)
    _apply_post_exit_streak(st, {"realized_pnl": 7.0})
    assert st.loss_recovery_streak == 0
    assert st.loss_recovery_multiplier == 1.0


def test_breakeven_exit_resets_streak():
    """realized_pnl == 0 counts as a (break-even) win → reset, not escalate."""
    st = DemoState(balance_usd=1000.0, loss_recovery_streak=2, loss_recovery_multiplier=2.0)
    _apply_post_exit_streak(st, {"realized_pnl": 0.0})
    assert st.loss_recovery_streak == 0
    assert st.loss_recovery_multiplier == 1.0


def test_none_realized_pnl_counts_as_loss_exit():
    """No realized PnL available → treated as a loss-exit (does NOT reset the streak)."""
    st = DemoState(balance_usd=1000.0, loss_recovery_streak=1, loss_recovery_multiplier=1.2)
    _apply_post_exit_streak(st, {"realized_pnl": None})
    assert st.loss_recovery_streak == 2
