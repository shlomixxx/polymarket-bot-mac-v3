"""מילוי settlement_btc לעסקאות TP אחרי סיום החלון — backfill ב-mark_to_market."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    import main as engine_main

    engine_main.demo.state_path = tmp_path / "demo_state.json"
    engine_main.demo.reset(10_000.0)
    engine_main.runner.rt.mode = "off"
    return TestClient(engine_main.app), engine_main


def test_mark_to_market_backfills_tp_settlement_after_window(client):
    tc, engine_main = client
    demo = engine_main.demo
    epoch = int(time.time()) - 10_000
    ws = 300
    demo.state.trades = [
        {
            "id": "buy1",
            "ts": time.time() - 500,
            "type": "BUY",
            "side": "Down",
            "session_id": "sess1",
            "contracts": 10,
            "price": 0.3,
            "epoch": epoch,
            "window_sec": ws,
            "token_id": "tok1",
        },
        {
            "id": "tp1",
            "ts": time.time() - 400,
            "type": "SELL_TP",
            "side": "Down",
            "session_id": "sess1",
            "contracts": 10,
            "price": 0.51,
            "epoch": epoch,
            "window_sec": ws,
            "token_id": "tok1",
            "realized_pnl": 1.0,
        },
    ]
    demo.save()

    fake_px = {"start": 66000.0, "end": 66100.0, "source": "binance_1m_proxy"}

    async def _fake_fetch(_ep: int, _ws: int):
        return dict(fake_px)

    with patch("btc_price.fetch_window_start_end_btc_usd", side_effect=_fake_fetch):
        r = tc.get("/api/demo/state")
    assert r.status_code == 200
    st = r.json()
    trades = st.get("trades") or []
    tp = next(t for t in trades if t.get("type") == "SELL_TP")
    assert tp.get("settlement_btc_start") == pytest.approx(66000.0)
    assert tp.get("settlement_btc_end") == pytest.approx(66100.0)
    assert tp.get("resolved_outcome") in ("Up", "Down")


def test_infer_prefers_first_buy_epoch_not_tp_epoch(tmp_path: Path):
    """אותו חלון 5m — epoch על TP (מ־tp_ctx) עלול להסטות; מקור האמת הוא BUY ראשון."""
    from demo_engine import DemoEngine

    eng = DemoEngine(state_path=tmp_path / "y.json")
    eng.reset(10_000.0)
    buy_epoch = 1_705_000_000
    tp_epoch_wrong = buy_epoch + 120
    eng.state.trades = [
        {
            "id": "b1",
            "type": "BUY",
            "side": "Down",
            "session_id": "sess",
            "ts": 100.0,
            "epoch": buy_epoch,
            "window_sec": 300,
        },
        {
            "id": "tp1",
            "type": "SELL_TP",
            "side": "Down",
            "session_id": "sess",
            "ts": 200.0,
            "epoch": tp_epoch_wrong,
            "window_sec": 300,
        },
    ]
    ep, ws = eng._infer_epoch_window_for_exit_trade(eng.state.trades[1])
    assert ep == buy_epoch
    assert ws == 300


@pytest.mark.asyncio
async def test_backfill_infer_epoch_from_buy_only(tmp_path: Path):
    from demo_engine import DemoEngine

    eng = DemoEngine(state_path=tmp_path / "x.json")
    eng.reset(10_000.0)
    epoch = int(time.time()) - 10_000
    eng.state.trades = [
        {
            "id": "buy1",
            "type": "BUY",
            "side": "Up",
            "session_id": "s2",
            "epoch": epoch,
            "window_sec": 300,
        },
        {
            "id": "tp2",
            "type": "SELL_TP",
            "side": "Up",
            "session_id": "s2",
            # בלי epoch על TP — רק מ-BUY
        },
    ]

    async def _fake(_ep: int, _ws: int):
        return {"start": 1.0, "end": 2.0, "source": "binance_1m_proxy"}

    with patch("btc_price.fetch_window_start_end_btc_usd", side_effect=_fake):
        await eng._backfill_missing_tp_settlement_btc()

    tp = eng.state.trades[1]
    assert tp.get("epoch") == epoch
    assert tp.get("settlement_btc_start") == 1.0
    assert tp.get("settlement_btc_end") == 2.0
