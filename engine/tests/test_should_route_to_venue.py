"""M2b-Step-3 — `_should_route_to_venue()`: route REAL orders to Predict.fun on TESTNET (fake
money) too, not only for real money (`_live_trading_ok()`).

Background: `_live_trading_ok()` for order_venue="predict_fun" delegates to
predict_secrets.is_live_enabled(), which requires MAINNET — so testnet Predict.fun trading was
previously silently dropped to demo-simulation (never routed to the venue at all, even though
the venue itself already submits real testnet orders once a wallet key is present — see
engine/venues/predict_fun.py + test_predict_fun_order_path.py). `_should_route_to_venue()` is
the new, broader gate used at every venue-vs-demo call site.

TDD:
  - Truth table: for order_venue="polymarket", `_should_route_to_venue()` MUST be
    byte-identical to `_live_trading_ok()` in every case (the CORE invariant — Predict.fun's
    testnet routing must never leak into the Polymarket/real-money path). `_live_trading_ok()`
    itself is NOT touched by this change.
  - predict_fun + testnet + wallet key + data_source=binance -> True (the actual fix).
  - Missing key / data_source mismatch / mainnet-not-fully-live -> False (fail-closed).
  - Routing: the auto-entry path in `_tick()` must actually call `self._venue.place_entry_order`
    (not `self.demo.simulate_market_buy`) once `_should_route_to_venue()` is True, and vice versa
    for the unchanged polymarket-non-live path.
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


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("POLYMARKET_LIVE", "POLYMARKET_PRIVATE_KEY", "PREDICT_LIVE",
              "PREDICT_WALLET_KEY", "PREDICT_TESTNET"):
        monkeypatch.delenv(v, raising=False)
    data_source.set_active("polymarket")
    yield
    data_source.set_active("polymarket")


def _runner(*, order_venue: str = "polymarket", live_trading: bool = True) -> StrategyRunner:
    r = StrategyRunner(DemoEngine())
    r.rt.config.order_venue = order_venue
    r.rt.live_trading = live_trading
    return r


# ── CORE INVARIANT: polymarket path stays byte-identical to _live_trading_ok() ─────────────────

def test_polymarket_true_case_is_byte_identical_to_live_trading_ok(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("polymarket")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    assert r._live_trading_ok() is True
    assert r._should_route_to_venue() is True


def test_polymarket_false_case_is_byte_identical_to_live_trading_ok(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=False)
    data_source.set_active("polymarket")
    assert r._live_trading_ok() is False
    assert r._should_route_to_venue() is False


def test_polymarket_kill_switch_false_case_is_byte_identical(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("polymarket")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setenv("POLYMARKET_LIVE", "0")
    assert r._live_trading_ok() is False
    assert r._should_route_to_venue() is False


def test_polymarket_byte_identical_even_with_predict_env_fully_unlocked(monkeypatch):
    """Predict.fun env vars / testnet logic must have ZERO bearing on the polymarket path —
    this is the whole point of the invariant (order_venue="polymarket" short-circuits
    _testnet_predict_active() to False regardless of everything else)."""
    r = _runner(order_venue="polymarket", live_trading=False)
    data_source.set_active("polymarket")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    monkeypatch.setenv("PREDICT_LIVE", "1")
    monkeypatch.setenv("PREDICT_TESTNET", "0")  # mainnet, fully live-enabled for predict_fun
    assert r._live_trading_ok() is False
    assert r._should_route_to_venue() is False


def test_polymarket_mismatch_guard_still_byte_identical(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("binance")  # MISMATCH: venue=polymarket, data=binance
    monkeypatch.setenv("POLYMARKET_LIVE", "1")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    assert r._live_trading_ok() is False
    assert r._should_route_to_venue() is False


# ── predict_fun + testnet: the actual fix — testnet routing now works ──────────────────────────

def test_predict_fun_testnet_with_key_and_matching_data_source_routes_true(monkeypatch):
    r = _runner(order_venue="predict_fun", live_trading=False)  # rt.live_trading OFF on purpose
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    # PREDICT_TESTNET unset -> default-safe testnet; PREDICT_LIVE unset -> not live-enabled
    assert r._live_trading_ok() is False  # confirm this is NOT the real-money path
    assert r._testnet_predict_active() is True
    assert r._should_route_to_venue() is True  # testnet-predict routing kicks in


def test_predict_fun_testnet_without_wallet_key_is_false(monkeypatch):
    r = _runner(order_venue="predict_fun", live_trading=False)
    data_source.set_active("binance")
    # no PREDICT_WALLET_KEY set
    assert r._testnet_predict_active() is False
    assert r._should_route_to_venue() is False


def test_predict_fun_testnet_with_key_but_data_source_mismatch_is_false(monkeypatch):
    r = _runner(order_venue="predict_fun", live_trading=False)
    data_source.set_active("polymarket")  # MISMATCH: venue=predict_fun, data=polymarket
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    assert r._testnet_predict_active() is False
    assert r._should_route_to_venue() is False


def test_predict_fun_mainnet_not_live_is_false(monkeypatch):
    """PREDICT_TESTNET=0 (mainnet) + not fully live-enabled: neither the real-money gate nor
    the testnet gate may pass — a half-open lock on mainnet must NOT silently fall back to
    'testnet routing' (predict_secrets.is_testnet() is False here, by design)."""
    r = _runner(order_venue="predict_fun", live_trading=False)
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    monkeypatch.setenv("PREDICT_TESTNET", "0")  # mainnet
    # PREDICT_LIVE left unset -> is_live_enabled() False
    assert r._live_trading_ok() is False
    assert r._testnet_predict_active() is False
    assert r._should_route_to_venue() is False


# ── Routing: the auto-entry path actually calls the venue (or demo), not both ──────────────────

TOKEN_UP = "tok_up"
TOKEN_DOWN = "tok_down"
_EPOCH = 424242


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
    r.rt.mode = "auto"           # run mode: enter directly, no pending_approval
    r.rt.current_epoch = _EPOCH  # match the fake market -> skip the rollover branch entirely
    r.rt._last_signal_refresh_ts = time.time()
    return r


async def _drive_tick(runner: StrategyRunner, *, ask_up: float, ask_down: float,
                       bid_up: float = 0.30, bid_down: float = 0.30):
    """Runs the REAL _tick() with only the network/venue seams mocked — mirrors
    test_decision_mode.py's harness, plus a fetch_portfolio stub so the unconditional
    _live_reconcile_if_enabled() call at the top of _tick() never touches the network."""

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


@pytest.mark.asyncio
async def test_testnet_predict_auto_entry_routes_to_venue_not_demo(tmp_path, monkeypatch):
    """The whole point of M2b-Step-3: with a testnet-predict config, the auto-entry path must
    call self._venue.place_entry_order (a REAL Predict.fun testnet order, fake money) and must
    NOT fall back to self.demo.simulate_market_buy."""
    cfg = _base_cfg(order_venue="predict_fun")
    r = _make_runner(tmp_path, cfg)
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    # PREDICT_TESTNET unset -> default-safe testnet; no PREDICT_LIVE -> real-money gate is False
    assert r._live_trading_ok() is False
    assert r._testnet_predict_active() is True

    place_entry_order = AsyncMock(return_value={"ok": True, "fill_price": 0.40, "price": 0.40})
    simulate_market_buy = AsyncMock(wraps=r.demo.simulate_market_buy)
    with patch.object(r._venue, "place_entry_order", place_entry_order), \
         patch.object(r.demo, "simulate_market_buy", simulate_market_buy):
        await _drive_tick(r, ask_up=0.40, ask_down=0.30)

    place_entry_order.assert_awaited_once()
    simulate_market_buy.assert_not_awaited()
    assert len(r.demo.state.positions) == 1


@pytest.mark.asyncio
async def test_polymarket_non_live_auto_entry_routes_to_demo_not_venue(tmp_path, monkeypatch):
    """Unchanged behavior: polymarket + not-live still demo-simulates, and never touches the
    venue's place_entry_order — the byte-identical invariant holds for the routing path too."""
    cfg = _base_cfg(order_venue="polymarket")
    r = _make_runner(tmp_path, cfg)
    data_source.set_active("polymarket")
    r.rt.live_trading = False
    assert r._should_route_to_venue() is False

    place_entry_order = AsyncMock(return_value={"ok": True, "fill_price": 0.40, "price": 0.40})
    simulate_market_buy = AsyncMock(wraps=r.demo.simulate_market_buy)
    with patch.object(r._venue, "place_entry_order", place_entry_order), \
         patch.object(r.demo, "simulate_market_buy", simulate_market_buy):
        await _drive_tick(r, ask_up=0.40, ask_down=0.30)

    simulate_market_buy.assert_awaited_once()
    place_entry_order.assert_not_awaited()
    assert len(r.demo.state.positions) == 1


# ── M2b fix-3: testnet-predict trades must be tagged execution="testnet" (fake money) — NOT
# execution="live" — so they never mix into the real "live statistics" view. ───────────────────

def test_execution_tag_truth_table(monkeypatch):
    """_execution_tag() mirrors _testnet_predict_active(): 'testnet' only for the fake-money
    Predict.fun testnet path, 'live' for everything else (including real Polymarket money)."""
    r = _runner(order_venue="predict_fun", live_trading=False)
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    assert r._testnet_predict_active() is True
    assert r._execution_tag() == "testnet"

    r2 = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("polymarket")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    assert r2._testnet_predict_active() is False
    assert r2._execution_tag() == "live"


@pytest.mark.asyncio
async def test_testnet_predict_auto_entry_tags_trade_execution_testnet(tmp_path, monkeypatch):
    """The trade record_live_buy writes for a testnet-predict auto-entry must carry
    execution='testnet' (fake money) — never 'live', which would leak into real live stats."""
    cfg = _base_cfg(order_venue="predict_fun")
    r = _make_runner(tmp_path, cfg)
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    assert r._testnet_predict_active() is True

    place_entry_order = AsyncMock(return_value={"ok": True, "fill_price": 0.40, "price": 0.40})
    with patch.object(r._venue, "place_entry_order", place_entry_order):
        await _drive_tick(r, ask_up=0.40, ask_down=0.30)

    assert len(r.demo.state.trades) == 1
    trade = r.demo.state.trades[-1]
    assert trade["execution"] == "testnet"
    # and it must NOT show up in the real live-stats CSV export
    assert str(trade["token_id"]) not in r.demo.export_csv(live_only=True)


# ── M2b fix-4 (semi-mode routing): approve_pending's default live_request must broaden to
# include testnet-predict, so an approved semi/manual-mode entry actually reaches the venue. ──

@pytest.mark.asyncio
async def test_approve_pending_buy_routes_testnet_predict_to_venue(tmp_path, monkeypatch):
    """Semi-mode: rt.live_trading is OFF (user never flipped the real-money toggle) but the
    venue is testnet-predict — approve_pending() must still route the approved buy to the
    venue (place_entry_order), not silently demo-simulate it, and must tag the resulting
    trade execution='testnet'."""
    cfg = _base_cfg(order_venue="predict_fun")
    r = _make_runner(tmp_path, cfg)
    r.select_venue("predict_fun")
    data_source.set_active("binance")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    r.rt.live_trading = False  # semi/manual mode: real-money toggle OFF
    assert r._testnet_predict_active() is True
    assert r._live_trading_ok() is False

    r.rt.pending_approval = {
        "action": "buy",
        "side": "Up",
        "contracts": 10.0,
        "token": TOKEN_UP,
        "limit": 0.40,
        "ask": 0.40,
        "context": {},
    }

    place_entry_order = AsyncMock(return_value={"ok": True, "fill_price": 0.40, "price": 0.40})
    simulate_market_buy = AsyncMock(wraps=r.demo.simulate_market_buy)
    with patch.object(r._venue, "place_entry_order", place_entry_order), \
         patch.object(r.demo, "simulate_market_buy", simulate_market_buy):
        result = await r.approve_pending()

    assert result.get("ok") is True
    place_entry_order.assert_awaited_once()
    simulate_market_buy.assert_not_awaited()
    assert r.demo.state.trades[-1]["execution"] == "testnet"


@pytest.mark.asyncio
async def test_approve_pending_buy_polymarket_non_live_still_routes_to_demo(tmp_path, monkeypatch):
    """Unchanged behavior: order_venue=polymarket + rt.live_trading=False still demo-simulates
    the approved buy — the broadened default must never affect the real-money path."""
    cfg = _base_cfg(order_venue="polymarket")
    r = _make_runner(tmp_path, cfg)
    data_source.set_active("polymarket")
    r.rt.live_trading = False
    assert r._testnet_predict_active() is False

    r.rt.pending_approval = {
        "action": "buy",
        "side": "Up",
        "contracts": 10.0,
        "token": TOKEN_UP,
        "limit": 0.40,
        "ask": 0.40,
        "context": {},
    }

    place_entry_order = AsyncMock(return_value={"ok": True, "fill_price": 0.40, "price": 0.40})
    simulate_market_buy = AsyncMock(wraps=r.demo.simulate_market_buy)
    with patch.object(r._venue, "place_entry_order", place_entry_order), \
         patch.object(r.demo, "simulate_market_buy", simulate_market_buy), \
         patch.object(r.demo, "best_ask", AsyncMock(return_value=0.40)):
        result = await r.approve_pending()

    assert result.get("ok") is True
    simulate_market_buy.assert_awaited_once()
    place_entry_order.assert_not_awaited()
    # demo-simulated trades never carry an "execution" tag at all (unaffected by this change)
    assert r.demo.state.trades[-1].get("execution") is None
