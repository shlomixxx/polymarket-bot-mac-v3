"""_live_trading_ok() must become venue-aware WITHOUT changing the Polymarket path:
  - order_venue == "polymarket": EXACT existing logic (POLYMARKET_LIVE kill-switch +
    rt.live_trading + POLYMARKET_PRIVATE_KEY), byte-identical behavior.
  - order_venue == "predict_fun": delegates to predict_secrets.is_live_enabled().
  - MISMATCH GUARD: never decide on one venue's oracle and execute on another —
    order_venue="predict_fun" requires data_source=="binance"; order_venue="polymarket"
    requires data_source=="polymarket". Any mismatch => False, regardless of locks.
"""
from __future__ import annotations

import pytest

import data_source
from demo_engine import DemoEngine
from strategy_runner import StrategyRunner


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


# ── polymarket + polymarket data_source: current behavior fully preserved ──
def test_polymarket_venue_requires_kill_switch_live_flag_and_key(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("polymarket")

    # POLYMARKET_LIVE=0 -> hard blocked regardless of everything else
    monkeypatch.setenv("POLYMARKET_LIVE", "0")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    assert r._live_trading_ok() is False

    # kill-switch clear, but rt.live_trading is off -> blocked
    monkeypatch.delenv("POLYMARKET_LIVE", raising=False)
    r.rt.live_trading = False
    assert r._live_trading_ok() is False

    # rt.live_trading on, but no private key -> blocked
    r.rt.live_trading = True
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    assert r._live_trading_ok() is False

    # all three satisfied -> enabled
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    assert r._live_trading_ok() is True


def test_polymarket_venue_kill_switch_accepts_falsey_variants(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("polymarket")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    for off in ("0", "false", "No", "OFF"):
        monkeypatch.setenv("POLYMARKET_LIVE", off)
        assert r._live_trading_ok() is False, off


# ── predict_fun + binance data_source: gated by predict_secrets.is_live_enabled() ──
def test_predict_fun_venue_gated_by_predict_secrets_triple_lock(monkeypatch):
    r = _runner(order_venue="predict_fun", live_trading=True)
    data_source.set_active("binance")

    # nothing set -> blocked
    assert r._live_trading_ok() is False

    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    monkeypatch.setenv("PREDICT_LIVE", "1")
    # still testnet by default -> blocked
    assert r._live_trading_ok() is False

    monkeypatch.setenv("PREDICT_TESTNET", "0")
    # all three satisfied -> enabled
    assert r._live_trading_ok() is True

    # PREDICT_LIVE flips off mid-session -> immediately refused
    monkeypatch.setenv("PREDICT_LIVE", "0")
    assert r._live_trading_ok() is False


def test_predict_fun_venue_ignores_polymarket_env_and_rt_live_trading_flag(monkeypatch):
    # Polymarket-side envs/flags must have zero effect on the predict_fun path.
    r = _runner(order_venue="predict_fun", live_trading=False)  # rt.live_trading OFF
    data_source.set_active("binance")
    monkeypatch.setenv("POLYMARKET_LIVE", "1")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    monkeypatch.setenv("PREDICT_LIVE", "1")
    monkeypatch.setenv("PREDICT_TESTNET", "0")
    assert r._live_trading_ok() is True  # predict_secrets alone decides


# ── MISMATCH GUARD: oracle-divergence must fail closed ──
def test_mismatch_predict_fun_venue_with_polymarket_data_source_blocked(monkeypatch):
    r = _runner(order_venue="predict_fun", live_trading=True)
    data_source.set_active("polymarket")  # MISMATCH: venue=predict_fun, data=polymarket
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    monkeypatch.setenv("PREDICT_LIVE", "1")
    monkeypatch.setenv("PREDICT_TESTNET", "0")
    # all three predict_secrets locks satisfied, but mismatch must still refuse
    assert r._live_trading_ok() is False


def test_mismatch_polymarket_venue_with_binance_data_source_blocked(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("binance")  # MISMATCH: venue=polymarket, data=binance
    monkeypatch.setenv("POLYMARKET_LIVE", "1")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    # all polymarket locks satisfied, but mismatch must still refuse
    assert r._live_trading_ok() is False


def test_matched_polymarket_venue_with_polymarket_data_source_not_blocked_by_guard(monkeypatch):
    r = _runner(order_venue="polymarket", live_trading=True)
    data_source.set_active("polymarket")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    assert r._live_trading_ok() is True


# ── NORMALIZATION: garbage order_venue normalizes to polymarket, then MISMATCH GUARD applies ──
def test_garbage_order_venue_normalizes_to_polymarket_then_mismatch_fails(monkeypatch):
    r = _runner(order_venue="GARBAGE", live_trading=True)
    data_source.set_active("binance")  # MISMATCH: normalized venue=polymarket, data=binance
    monkeypatch.setenv("POLYMARKET_LIVE", "1")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xkey")
    # garbage normalizes to polymarket; polymarket+binance is a mismatch → blocked
    assert r._live_trading_ok() is False
