"""Decouple market-DATA reads from the order-EXECUTION venue.

BUG (verified): the strategy runner reads its order book from `self._venue`, which at
startup is PredictFunVenue when config `order_venue=predict_fun`. The Predict.fun TESTNET
book is EMPTY (bestAsk/bestBid = None on every btc-updown market), so the strategy bailed
at the "Ask חסר" gate every tick and NEVER opened a demo position — silently dead since the
venue switch. The Trigger engine was unaffected because it reads the liquid Polymarket CLOB
directly.

FIX: a `book_venue` used ONLY for price/book READS, chosen as
    book_venue = self._venue  if _should_route_to_venue()  else  <polymarket data venue>
  - routing True  (real money OR testnet-predict): read from the SAME venue an order would
    hit — honest; if its book is empty, no fabricated fill.
  - routing False (plain DEMO, orders simulated): read from the liquid Polymarket book so the
    strategy can still form entries even when order_venue=predict_fun.
ORDER EXECUTION stays on `self._venue`, still gated by `_should_route_to_venue()`.
"""
from __future__ import annotations

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


def _fake_market() -> ActiveMarket:
    return ActiveMarket(
        slug="btc-updown-5m-424242",
        epoch=_EPOCH,
        condition_id="cond",
        end_date_iso="2026-06-08T00:00:00Z",
        closed=False,
        token_up=TOKEN_UP,
        token_down=TOKEN_DOWN,
        outcome_prices=(0.5, 0.5),
        order_min_size=5.0,
        title="BTC Up/Down",
        window_sec=300,
    )


def _base_cfg(**overrides) -> StrategyConfig:
    defaults = dict(
        order_mode="market",
        market_max_entry_price_cents=100.0,  # no price ceiling
        entry_price_cents=50.0,
        investment_usd=20.0,
        min_contracts=5,
        min_minutes_for_entry=3.0,
        freeze_last_minutes=1.0,
        take_profit_pct=20.0,
    )
    defaults.update(overrides)
    return StrategyConfig(**defaults)


def _make_runner(tmp_path: Path, cfg: StrategyConfig) -> StrategyRunner:
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    r = StrategyRunner(demo=eng)
    r.rt.config = cfg
    r.rt.mode = "auto"
    r.rt.current_epoch = _EPOCH  # match the fake market -> skip the rollover branch
    r.rt._last_signal_refresh_ts = time.time()
    return r


async def _poly_bid_ask(token_id: str):
    if token_id == TOKEN_UP:
        return (0.30, 0.40)
    if token_id == TOKEN_DOWN:
        return (0.30, 0.30)
    return (None, None)


async def _poly_best_ask(token_id: str):
    if token_id == TOKEN_UP:
        return 0.40
    if token_id == TOKEN_DOWN:
        return 0.30
    return None


# ── unit: _book_venue() picks the right venue for READS ─────────────────────────────────────────

def test_book_venue_is_polymarket_in_demo(tmp_path):
    r = _make_runner(tmp_path, _base_cfg(order_venue="predict_fun"))
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    assert r._should_route_to_venue() is False           # plain demo
    assert r._venue is not r._polymarket_data_venue       # distinct instances
    assert r._book_venue() is r._polymarket_data_venue    # reads use the liquid Polymarket book


def test_book_venue_is_execution_venue_when_routing(tmp_path, monkeypatch):
    r = _make_runner(tmp_path, _base_cfg(order_venue="predict_fun"))
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")     # testnet-predict routing ON
    assert r._should_route_to_venue() is True
    assert r._book_venue() is r._venue                    # honest: read the venue we route to


# ── behavior: DEMO + order_venue=predict_fun reads Polymarket book and OPENS an entry ───────────

@pytest.mark.asyncio
async def test_demo_predict_fun_reads_polymarket_book_and_enters(tmp_path):
    """THE BUG: order_venue=predict_fun + demo. Predict.fun book is EMPTY (None,None); the
    Polymarket book is liquid. The strategy must READ the Polymarket book and form an entry.
    FAILS before the fix (reads the empty Predict.fun book -> bails at the "Ask חסר" gate)."""
    cfg = _base_cfg(order_venue="predict_fun")
    r = _make_runner(tmp_path, cfg)
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    assert r._should_route_to_venue() is False            # demo: orders simulated, not routed

    predict_bid_ask = AsyncMock(return_value=(None, None))   # empty testnet book
    poly_bid_ask = AsyncMock(side_effect=_poly_bid_ask)      # liquid book
    fake_market = _fake_market()
    with patch.object(r._venue, "discover_active_window", AsyncMock(return_value=fake_market)), \
         patch.object(r._venue, "best_bid_ask", predict_bid_ask), \
         patch.object(r._polymarket_data_venue, "discover_active_window",
                      AsyncMock(return_value=fake_market)), \
         patch.object(r._polymarket_data_venue, "best_bid_ask", poly_bid_ask), \
         patch.object(strategy_runner, "seconds_until_window_end", return_value=200), \
         patch.object(r.demo, "mark_to_market", AsyncMock(return_value={})), \
         patch.object(r.demo, "best_ask", side_effect=_poly_best_ask):
        await r._tick()

    poly_bid_ask.assert_awaited()          # book reads went to the liquid Polymarket venue
    predict_bid_ask.assert_not_awaited()   # never read the empty Predict.fun book
    assert len(r.demo.state.positions) == 1  # entry actually opened


# ── behavior: testnet ROUTING reads the real (empty) Predict.fun book — no fake liquidity ───────

@pytest.mark.asyncio
async def test_testnet_routing_reads_predict_book_not_polymarket(tmp_path, monkeypatch):
    """When an order WOULD route (testnet-predict active), reads must stay on the Predict.fun
    venue (self._venue). Its book is empty -> the strategy bails, forms no entry, and the
    liquid Polymarket book is NEVER consulted for reads — we don't fabricate testnet liquidity."""
    cfg = _base_cfg(order_venue="predict_fun")
    r = _make_runner(tmp_path, cfg)
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    assert r._should_route_to_venue() is True

    predict_bid_ask = AsyncMock(return_value=(None, None))
    poly_bid_ask = AsyncMock(side_effect=_poly_bid_ask)
    place_entry_order = AsyncMock(return_value={"ok": True, "fill_price": 0.4, "price": 0.4})
    fake_market = _fake_market()
    with patch.object(r._venue, "discover_active_window", AsyncMock(return_value=fake_market)), \
         patch.object(r._venue, "best_bid_ask", predict_bid_ask), \
         patch.object(r._venue, "fetch_portfolio", AsyncMock(return_value={"ok": False})), \
         patch.object(r._venue, "place_entry_order", place_entry_order), \
         patch.object(r._polymarket_data_venue, "discover_active_window",
                      AsyncMock(return_value=fake_market)), \
         patch.object(r._polymarket_data_venue, "best_bid_ask", poly_bid_ask), \
         patch.object(strategy_runner, "seconds_until_window_end", return_value=200), \
         patch.object(r.demo, "mark_to_market", AsyncMock(return_value={})), \
         patch.object(r.demo, "best_ask", side_effect=_poly_best_ask):
        await r._tick()

    predict_bid_ask.assert_awaited()          # reads stayed on the Predict.fun venue
    poly_bid_ask.assert_not_awaited()         # liquid Polymarket book never consulted
    place_entry_order.assert_not_awaited()    # empty book -> no order
    assert len(r.demo.state.positions) == 0   # no fabricated entry
