"""
בדיקות ליחידת reconcile_live_state ב-DemoEngine + סגירה אקטיבית בזמן rollover.
"""
from pathlib import Path

import pytest

from demo_engine import DemoEngine, Position


@pytest.fixture()
def eng(tmp_path: Path) -> DemoEngine:
    e = DemoEngine()
    e.state_path = tmp_path / "demo_state.json"
    e.reset(100.0)
    return e


# ─────────────────── reconcile_live_state ───────────────────


def test_reconcile_creates_delta_trade(eng: DemoEngine):
    """כשיש פער ≥ $0.01 בין יתרת shadow ל-real → מייצר RECONCILE trade."""
    eng.state.balance_usd = 100.0
    tr = eng.reconcile_live_state(
        real_balance_usd=97.0,
        real_positions=[],
    )
    assert tr is not None
    assert tr["type"] == "RECONCILE"
    assert abs(tr["realized_pnl"] - (-3.0)) < 1e-9
    assert eng.state.balance_usd == 97.0
    assert any(t["type"] == "RECONCILE" for t in eng.state.trades)


def test_reconcile_no_trade_on_tiny_delta(eng: DemoEngine):
    """פער < $0.01 = אין RECONCILE trade."""
    eng.state.balance_usd = 100.0
    tr = eng.reconcile_live_state(
        real_balance_usd=100.005,
        real_positions=[],
    )
    assert tr is None
    assert eng.state.balance_usd == 100.005
    assert not any(t.get("type") == "RECONCILE" for t in eng.state.trades)


def test_reconcile_rebuilds_positions(eng: DemoEngine):
    """פוזיציות shadow מוחלפות בפוזיציות real; טוקנים שנעלמו → tracking נמחק."""
    # shadow: token A
    eng.state.positions = [
        Position(side="Up", contracts=10, avg_cost=0.55, token_id="A"),
    ]
    eng._position_tracking["A"] = {"open_ts": 0}
    eng._session_by_token["A"] = "session-a"

    # real: token B (A already sold on-chain)
    tr = eng.reconcile_live_state(
        real_balance_usd=100.0,
        real_positions=[
            {"token_id": "B", "side": "Down", "size": 5.0, "avg_price": 0.42},
        ],
    )
    assert len(eng.state.positions) == 1
    p = eng.state.positions[0]
    assert p.token_id == "B"
    assert p.side == "Down"
    assert p.contracts == 5.0
    assert p.avg_cost == 0.42
    # A tracking/session cleaned up
    assert "A" not in eng._position_tracking
    assert "A" not in eng._session_by_token


def test_reconcile_preserves_shadow_avg_cost(eng: DemoEngine):
    """אם token קיים בשתי הרשימות — עדיפות ל-avg_cost מהצל (כולל fee)."""
    eng.state.positions = [
        Position(side="Up", contracts=10, avg_cost=0.60, token_id="T"),
    ]
    tr = eng.reconcile_live_state(
        real_balance_usd=100.0,
        real_positions=[
            {"token_id": "T", "side": "Up", "size": 12.0, "avg_price": 0.55},
        ],
    )
    assert len(eng.state.positions) == 1
    p = eng.state.positions[0]
    assert p.token_id == "T"
    assert p.avg_cost == 0.60  # shadow preserved, not overwritten by API's avg_price
    assert p.contracts == 12.0  # size from real


def test_reconcile_with_none_balance(eng: DemoEngine):
    """real_balance_usd=None → לא משנה את היתרה."""
    eng.state.balance_usd = 100.0
    tr = eng.reconcile_live_state(
        real_balance_usd=None,
        real_positions=[],
    )
    assert tr is None
    assert eng.state.balance_usd == 100.0


def test_reconcile_positive_delta(eng: DemoEngine):
    """דלתא חיובית (רווח שלא נרשם בצל) גם נרשמת כ-RECONCILE."""
    eng.state.balance_usd = 90.0
    tr = eng.reconcile_live_state(
        real_balance_usd=95.5,
        real_positions=[],
    )
    assert tr is not None
    assert tr["type"] == "RECONCILE"
    assert abs(tr["realized_pnl"] - 5.5) < 1e-9
    assert eng.state.balance_usd == 95.5
