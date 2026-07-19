"""Audit ledger venue provenance: every recorded audit row's context_json MUST carry
`data_source` and `order_venue` so the "ביקורת עסקאות" ledger can tell Binance/Predict.fun
trades apart from Polymarket ones.

Root cause of the defect: strategy_runner assembled base_ctx["audit_inputs"] WITHOUT the
active venue/data_source, so those never rode into audit_tracker's context_json (0 of the
existing rows mention a venue). This drives the REAL _tick() -> demo BUY hook -> open_row
path (same harness as test_should_route_to_venue.py) and reads the row back to prove the
provenance is now recorded. It is ADDITIVE metadata only — no schema change, no trade change.
"""
from __future__ import annotations

import importlib
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import data_source
import strategy_runner
from strategy_runner import StrategyConfig, StrategyRunner
from demo_engine import DemoEngine, DemoState
from market_discovery import ActiveMarket


TOKEN_UP = "tok_up"
TOKEN_DOWN = "tok_down"
_EPOCH = 424242


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("POLYMARKET_LIVE", "POLYMARKET_PRIVATE_KEY", "PREDICT_LIVE",
              "PREDICT_WALLET_KEY", "PREDICT_TESTNET"):
        monkeypatch.delenv(v, raising=False)
    data_source.set_active("polymarket")
    yield
    data_source.set_active("polymarket")


def _fresh_audit_tracker(tmp_path, monkeypatch):
    """Isolate audit.db to a temp DATA_ROOT (mirrors test_audit_tracker._fresh_tracker).

    The demo BUY hook does `import audit_tracker` at call time, resolving the SAME reloaded
    module object from sys.modules, so it writes to the temp DB too."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import audit_tracker
    importlib.reload(audit_tracker)
    return audit_tracker


def _fake_market() -> ActiveMarket:
    return ActiveMarket(
        slug="btc-updown-5m-424242", epoch=_EPOCH, condition_id="cond",
        end_date_iso="2026-06-08T00:00:00Z", closed=False,
        token_up=TOKEN_UP, token_down=TOKEN_DOWN, outcome_prices=(0.5, 0.5),
        order_min_size=5.0, title="BTC Up/Down", window_sec=300,
    )


def _base_cfg(**overrides) -> StrategyConfig:
    defaults = dict(
        order_mode="market", market_max_entry_price_cents=100.0, entry_price_cents=50.0,
        investment_usd=20.0, min_contracts=5, min_minutes_for_entry=3.0,
        freeze_last_minutes=1.0, take_profit_pct=20.0,
    )
    defaults.update(overrides)
    return StrategyConfig(**defaults)


def _make_runner(tmp_path: Path, cfg: StrategyConfig) -> StrategyRunner:
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    r = StrategyRunner(demo=eng)
    r.rt.config = cfg
    r.rt.mode = "auto"
    r.rt.current_epoch = _EPOCH
    r.rt._last_signal_refresh_ts = time.time()
    return r


async def _drive_tick(runner: StrategyRunner, *, ask_up: float, ask_down: float,
                      bid_up: float = 0.30, bid_down: float = 0.30):
    async def fake_bid_ask(token_id: str):
        if token_id == TOKEN_UP:
            return (bid_up, ask_up)
        if token_id == TOKEN_DOWN:
            return (bid_down, ask_down)
        return (None, None)

    async def fake_best_ask(token_id: str):
        if token_id == TOKEN_UP:
            return ask_up
        if token_id == TOKEN_DOWN:
            return ask_down
        return None

    with patch.object(runner._venue, "discover_active_window",
                      AsyncMock(return_value=_fake_market())), \
         patch.object(runner._venue, "best_bid_ask", side_effect=fake_bid_ask), \
         patch.object(runner._venue, "fetch_portfolio",
                      AsyncMock(return_value={"ok": False})), \
         patch.object(strategy_runner, "seconds_until_window_end", return_value=200), \
         patch.object(runner.demo, "mark_to_market", AsyncMock(return_value={})), \
         patch.object(runner.demo, "best_ask", side_effect=fake_best_ask):
        await runner._tick()


def _recorded_context(audit_tracker):
    rows = audit_tracker.list_audits()
    assert rows, "expected at least one audit row recorded by the BUY hook"
    return rows[0].get("context") or {}


@pytest.mark.asyncio
async def test_demo_polymarket_buy_records_venue_provenance(tmp_path, monkeypatch):
    """Default Polymarket demo path: context_json must carry data_source/order_venue."""
    at = _fresh_audit_tracker(tmp_path, monkeypatch)
    cfg = _base_cfg(order_venue="polymarket")
    r = _make_runner(tmp_path, cfg)
    data_source.set_active("polymarket")
    r.rt.live_trading = False

    await _drive_tick(r, ask_up=0.40, ask_down=0.30)

    assert len(r.demo.state.positions) == 1
    ctx = _recorded_context(at)
    assert ctx.get("data_source") == "polymarket"
    assert ctx.get("order_venue") == "polymarket"


@pytest.mark.asyncio
async def test_predict_fun_binance_buy_records_venue_provenance(tmp_path, monkeypatch):
    """The actual defect: a Predict.fun (Binance data) trade must be distinguishable in the
    ledger — context_json data_source='binance', order_venue='predict_fun'."""
    at = _fresh_audit_tracker(tmp_path, monkeypatch)
    cfg = _base_cfg(order_venue="predict_fun")
    r = _make_runner(tmp_path, cfg)
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    assert r._testnet_predict_active() is True

    place_entry_order = AsyncMock(return_value={"ok": True, "fill_price": 0.40, "price": 0.40})
    with patch.object(r._venue, "place_entry_order", place_entry_order):
        await _drive_tick(r, ask_up=0.40, ask_down=0.30)

    assert len(r.demo.state.positions) == 1
    ctx = _recorded_context(at)
    assert ctx.get("data_source") == "binance"
    assert ctx.get("order_venue") == "predict_fun"
