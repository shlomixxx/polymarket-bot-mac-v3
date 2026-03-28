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
        await eng.expire_all_outside_tokens(
            ("keep", "other"),
            context={"settled_epoch": 1_700_000_000, "settled_window_sec": 300},
        )

    assert [p.token_id for p in eng.state.positions] == ["keep"]
    settle_loss = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(settle_loss) == 1
    t = settle_loss[0]
    assert t["token_id"] == "old"
    assert float(t["price"]) == 0.0
    # הפסד מלא של העלות (כולל עמלה בקירוב)
    expected_loss = -(0.4 * 10.0 * (1 + FEE_RATE))
    assert float(t["realized_pnl"]) == pytest.approx(expected_loss)


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

