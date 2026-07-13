"""Tests for decision_mode (manual/suggest/auto) — let the SIGNAL pick the entry side.

DEFAULT is "manual" → byte-identical to the current FLW → side_preference → cheaper-ask
cascade. These tests drive the real StrategyRunner._tick with the network seams mocked
(discovery, book fetch, time-left, mark_to_market, demo.best_ask) so the WHOLE side-selection
cascade + the suggest→pending_approval + auto-entry routing is exercised end-to-end.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import strategy_runner
from strategy_runner import StrategyConfig, StrategyRunner
from demo_engine import DemoEngine, DemoState
from market_discovery import ActiveMarket


TOKEN_UP = "tok_up"
TOKEN_DOWN = "tok_down"


def _fake_market() -> ActiveMarket:
    return ActiveMarket(
        slug="btc-updown-5m-12345",
        epoch=12345,
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


def _make_runner(tmp_path: Path, cfg: StrategyConfig) -> StrategyRunner:
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    r = StrategyRunner(demo=eng)
    r.rt.config = cfg
    r.rt.mode = "auto"          # run mode (separate from cfg.decision_mode)
    r.rt.current_epoch = 12345  # match market → no rollover branch
    # Suppress the audit signal-refresh so our injected _last_signal_result is not overwritten.
    r.rt._last_signal_refresh_ts = time.time()
    return r


async def _drive_tick(runner: StrategyRunner, *, ask_up: float, ask_down: float,
                      bid_up: float = 0.30, bid_down: float = 0.30):
    """Run _tick once with all network seams mocked. Down is CHEAPER by default
    (so manual/cheaper-ask would pick Down) unless the caller flips the asks."""

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
         patch.object(strategy_runner, "seconds_until_window_end", return_value=200), \
         patch.object(runner.demo, "mark_to_market", AsyncMock(return_value={})), \
         patch.object(runner.demo, "best_ask", side_effect=fake_best_ask):
        await runner._tick()


def _base_cfg(**overrides) -> StrategyConfig:
    """A cfg that enters on the first window in demo: market order_mode (marketable fill),
    no DCA/hedge/FLW. side_preference defaults to 'Up' but we set 'signal' for the
    cheaper-ask manual-baseline test where relevant."""
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


# ── config defaults / round-trip ────────────────────────────────────────────

def test_decision_mode_default_is_manual():
    cfg = StrategyConfig()
    assert cfg.decision_mode == "manual"
    assert cfg.decision_min_confidence == 60.0


def test_decision_mode_settable():
    cfg = StrategyConfig(decision_mode="auto", decision_min_confidence=75.0)
    assert cfg.decision_mode == "auto"
    assert cfg.decision_min_confidence == 75.0


# ── manual (default): cheaper-ask wins, byte-identical to current behavior ────

@pytest.mark.asyncio
async def test_manual_signal_pref_picks_cheaper_ask(tmp_path):
    """decision_mode=manual + side_preference=signal → cheaper-ask (Down) wins,
    EVEN IF a confident Up signal is present (signal must be ignored in manual)."""
    cfg = _base_cfg(decision_mode="manual", side_preference="signal")
    r = _make_runner(tmp_path, cfg)
    r.rt._last_signal_result = {"recommendation": "Up", "confidence_pct": 90.0}
    # Down is cheaper (0.30 < 0.40) → cheaper-ask picks Down.
    await _drive_tick(r, ask_up=0.40, ask_down=0.30)
    assert r.rt.pending_approval is None
    assert len(r.demo.state.positions) == 1
    assert r.demo.state.positions[0].side == "Down"


# ── auto: signal picks the side even when the other side is cheaper ───────────

@pytest.mark.asyncio
async def test_auto_confident_signal_picks_recommended_side(tmp_path):
    """decision_mode=auto + confident Up signal → enters Up even though Down is cheaper."""
    cfg = _base_cfg(decision_mode="auto", decision_min_confidence=60.0,
                    side_preference="signal")
    r = _make_runner(tmp_path, cfg)
    r.rt._last_signal_result = {"recommendation": "Up", "confidence_pct": 70.0}
    # Down cheaper → if the signal were ignored, manual cheaper-ask would pick Down.
    await _drive_tick(r, ask_up=0.40, ask_down=0.30)
    assert r.rt.pending_approval is None  # auto enters directly, no approval gate
    assert len(r.demo.state.positions) == 1
    assert r.demo.state.positions[0].side == "Up"


@pytest.mark.asyncio
async def test_auto_low_confidence_skips_entry(tmp_path):
    """decision_mode=auto + low-confidence signal → SKIP (no order, no pending)."""
    cfg = _base_cfg(decision_mode="auto", decision_min_confidence=60.0,
                    side_preference="signal")
    r = _make_runner(tmp_path, cfg)
    r.rt._last_signal_result = {"recommendation": "Up", "confidence_pct": 55.0}
    await _drive_tick(r, ask_up=0.40, ask_down=0.30)
    assert r.rt.pending_approval is None
    assert len(r.demo.state.positions) == 0  # skipped — did NOT fall back to cheaper-ask


@pytest.mark.asyncio
async def test_auto_neutral_signal_skips_entry(tmp_path):
    """decision_mode=auto + neutral signal → SKIP (no order, no pending)."""
    cfg = _base_cfg(decision_mode="auto", decision_min_confidence=60.0,
                    side_preference="signal")
    r = _make_runner(tmp_path, cfg)
    r.rt._last_signal_result = {"recommendation": "neutral", "confidence_pct": 80.0}
    await _drive_tick(r, ask_up=0.40, ask_down=0.30)
    assert r.rt.pending_approval is None
    assert len(r.demo.state.positions) == 0


@pytest.mark.asyncio
async def test_auto_no_signal_skips_entry(tmp_path):
    """decision_mode=auto with no cached signal at all → SKIP."""
    cfg = _base_cfg(decision_mode="auto", side_preference="signal")
    r = _make_runner(tmp_path, cfg)
    r.rt._last_signal_result = None
    await _drive_tick(r, ask_up=0.40, ask_down=0.30)
    assert r.rt.pending_approval is None
    assert len(r.demo.state.positions) == 0


# ── suggest: confident signal → pending_approval (NOT an immediate order) ─────

@pytest.mark.asyncio
async def test_suggest_confident_signal_creates_pending_approval(tmp_path):
    """decision_mode=suggest + confident Down signal → pending_approval with the signal's
    side is created (regardless of run mode=auto); NO immediate order."""
    cfg = _base_cfg(decision_mode="suggest", decision_min_confidence=60.0,
                    side_preference="signal")
    r = _make_runner(tmp_path, cfg)
    # Up is cheaper here; signal says Down → proves the signal (not cheaper-ask) drives it.
    r.rt._last_signal_result = {"recommendation": "Down", "confidence_pct": 80.0}
    await _drive_tick(r, ask_up=0.30, ask_down=0.40)
    assert r.rt.pending_approval is not None
    assert r.rt.pending_approval["side"] == "Down"
    assert r.rt.pending_approval["action"] == "buy"
    assert r.rt.pending_approval["token"] == TOKEN_DOWN
    # No position was opened — it awaits the user's approval.
    assert len(r.demo.state.positions) == 0
    # Audit context records the decision-mode + signal.
    ctx = r.rt.pending_approval["context"]
    assert ctx.get("decision_mode") == "suggest"
    assert ctx.get("decision_signal_rec") == "Down"
    assert ctx.get("decision_signal_confidence_pct") == 80.0


@pytest.mark.asyncio
async def test_suggest_low_confidence_skips_entry(tmp_path):
    """decision_mode=suggest + low-confidence → SKIP (no pending, no order)."""
    cfg = _base_cfg(decision_mode="suggest", decision_min_confidence=60.0,
                    side_preference="signal")
    r = _make_runner(tmp_path, cfg)
    r.rt._last_signal_result = {"recommendation": "Down", "confidence_pct": 50.0}
    await _drive_tick(r, ask_up=0.30, ask_down=0.40)
    assert r.rt.pending_approval is None
    assert len(r.demo.state.positions) == 0
