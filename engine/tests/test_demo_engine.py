import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from demo_engine import DemoEngine, DemoState, FEE_RATE, Position


@pytest.mark.asyncio
async def test_mark_to_market_no_positions_does_not_append_equity_history(tmp_path: Path):
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1234.0, positions=[], trades=[], equity_history=[])
    before = list(eng.state.equity_history)
    mark = await eng.mark_to_market()
    after = list(eng.state.equity_history)

    assert before == after
    assert mark["equity"] == pytest.approx(1234.0)
    assert mark["unrealized_usd"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_expire_all_outside_tokens_creates_expire_trade_and_removes_position(tmp_path: Path):
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    eng.state.positions = [
        Position(side="Up", contracts=10.0, avg_cost=0.4, token_id="old"),
        Position(side="Down", contracts=5.0, avg_cost=0.2, token_id="keep"),
    ]

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 99.0, "source": "binance_1m_proxy"}

    with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
        created = await eng.expire_all_outside_tokens(
            ("keep", "other"),
            context={
                "settled_epoch": 1_700_000_000,
                "settled_window_sec": 300,
                "execution": "live",
            },
        )

    assert len(created) == 1
    assert [p.token_id for p in eng.state.positions] == ["keep"]
    settle_loss = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(settle_loss) == 1
    t = settle_loss[0]
    assert created[0]["id"] == t["id"]
    assert t["token_id"] == "old"
    assert float(t["price"]) == 0.0
    # הפסד מלא של העלות (כולל עמלה בקירוב)
    expected_loss = -(0.4 * 10.0 * (1 + FEE_RATE))
    assert float(t["realized_pnl"]) == pytest.approx(expected_loss)
    assert t.get("execution") == "live"
    # leg_cost נחתם על עסקת הסגירה (מזין realized_pct ב-audit finalize)
    assert float(t["leg_cost"]) == pytest.approx(0.4 * 10.0 * (1 + FEE_RATE))


@pytest.mark.asyncio
async def test_expire_keeps_position_whose_window_not_ended(tmp_path: Path):
    """הגנת flap: פוזיציה שהחלון שלה עדיין פעיל (לפי שעון) לא מותחשבנת מוקדם,
    גם אם הטוקן שלה אינו בחלון שהתגלה כרגע. פוזיציה שחלונה הסתיים — כן מותחשבנת.
    זה מונע את ריבוי ההפסדים באותו חלון."""
    eng = DemoEngine(state_path=tmp_path / "s.json")
    eng.state = DemoState(balance_usd=1000.0)
    now = time.time()
    active_ep = int(now // 300 * 300)  # תחילת החלון הנוכחי — נגמר ב-active_ep+300 (עתיד)
    eng.state.positions = [
        Position(side="Down", contracts=10.0, avg_cost=0.4, token_id="active_tok",
                 window_epoch=active_ep, window_sec=300),
        Position(side="Up", contracts=5.0, avg_cost=0.5, token_id="old_tok",
                 window_epoch=active_ep - 600, window_sec=300),  # חלון שהסתיים
    ]

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 99.0, "source": "test"}

    with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
        created = await eng.expire_all_outside_tokens(
            ("other_up", "other_down"),
            context={"settled_epoch": active_ep, "settled_window_sec": 300},
        )

    remaining = [p.token_id for p in eng.state.positions]
    assert "active_tok" in remaining, "פוזיציה בחלון פעיל לא אמורה להתחשבן מוקדם"
    assert "old_tok" not in remaining, "פוזיציה שחלונה הסתיים אמורה להתחשבן"
    assert any(t["token_id"] == "old_tok" for t in created)
    assert not any(t.get("token_id") == "active_tok" for t in created)


@pytest.mark.asyncio
async def test_expire_marks_reconcile_origin_when_no_session_id(tmp_path: Path):
    """פוזיציה שנכנסה דרך reconcile (חסרה ב-_session_by_token) חייבת להיות מסומנת
    reconcile_origin=True ב-SETTLE — כדי שה-UI לא יציג אותה כ«עסקה» ריקה של הריצה."""
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    eng.state.positions = [
        Position(side="Up", contracts=10.0, avg_cost=0.4, token_id="ghost-from-chain"),
        Position(side="Down", contracts=5.0, avg_cost=0.2, token_id="bot-opened"),
    ]
    # רק הטוקן שנפתח ע״י ה-BOT — יש לו session_id
    eng._session_by_token = {"bot-opened": "sess-abc"}

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 99.0, "source": "binance_1m_proxy"}

    with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
        created = await eng.expire_all_outside_tokens(
            ("other_up", "other_down"),
            context={
                "settled_epoch": 1_700_000_000,
                "settled_window_sec": 300,
                "execution": "live",
            },
        )

    assert len(created) == 2
    by_tid = {t["token_id"]: t for t in created}
    # הפוזיציה מה-chain (ללא session_id) — מסומנת reconcile_origin
    assert by_tid["ghost-from-chain"].get("reconcile_origin") is True
    assert by_tid["ghost-from-chain"].get("session_id") is None
    # הפוזיציה של ה-BOT — נשארת רגילה עם session_id
    assert by_tid["bot-opened"].get("reconcile_origin") is None
    assert by_tid["bot-opened"].get("session_id") == "sess-abc"


def test_export_csv_contains_headers_and_snapshot(tmp_path: Path):
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=10_000.0)
    eng.state.trades = [
        {"ts": time.time(), "type": "EXPIRE_0", "side": "Up", "contracts": 1, "price": 0, "fee_est": 0, "token_id": "x", "realized_pnl": -1.0}
    ]
    eng.state.last_mark = {"ts": time.time(), "equity": 9999.0, "unrealized_usd": 0.0}
    csv_text = eng.export_csv()

    assert "ts,time,type,side,contracts,price,fee_est,token_id,session_id,realized_pnl" in csv_text
    assert "snapshot_ts,equity,unrealized_usd" in csv_text


def test_audit_buy_hook_creates_row_and_excludes_audit_inputs(tmp_path: Path, monkeypatch):
    """The BUY audit hook must (a) open an audit row keyed by session_id, and (b) NEVER let
    audit_inputs ride onto the persisted trade dict (invariant: keep demo_state.json lean)."""
    import importlib
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import audit_tracker
    importlib.reload(audit_tracker)  # bind audit.db to the temp DATA_ROOT

    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1_000.0)
    audit_inputs = {
        "mode": "live", "slug": "s", "epoch": 1, "window_sec": 300, "code_version": "t",
        "signal_result": None, "policy": {"loss_recovery_multiplier": 1.0},
        "book": {"ask_u": 0.5, "bid_u": 0.48, "ask_d": 0.5, "bid_d": 0.48},
        "provenance": {}, "regime": {"vol_bucket": "mid"},
    }
    eng.record_live_buy("Up", "tok-A", 10.0, 0.5,
                        context={"audit_inputs": audit_inputs, "gate": "test"})

    persisted = eng.state.trades[-1]
    assert "audit_inputs" not in persisted          # (b) not persisted onto the trade
    sid = persisted["session_id"]
    row = audit_tracker.get_audit(sid)
    assert row is not None                            # (a) audit row opened
    assert row["side"] == "Up"
    assert row["window_sec"] == 300
    assert row["settlement_status"] == "PENDING"

