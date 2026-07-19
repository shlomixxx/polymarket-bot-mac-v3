"""
Trigger ("quick-trade") entries must route through the SAME post-crash risk guards the
strategy runner uses, so they can no longer bypass them.

Background: trigger_engine._execute_trade fills via demo.simulate_market_buy WITHOUT calling
the strategy runner's _entry_limits_ok, which houses:
  (1) the self-recovering circuit-breaker, and
  (2) the 25%-of-balance notional cap (MAX_ENTRY_FRACTION_OF_BALANCE).

These tests assert that a Trigger entry is BLOCKED (no fill) when it exceeds 25% of balance
or when the circuit-breaker is tripped, while a within-limits entry still succeeds unchanged.
The guards are reused from the runner (shared demo + strategy config) — not duplicated.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_market(epoch: int = 1_000_000, oms: int = 5):
    m = MagicMock()
    m.epoch = epoch
    m.token_up = "up_tok"
    m.token_down = "down_tok"
    m.window_sec = 300
    m.order_min_size = oms
    m.slug = f"btc-updown-5m-{epoch}"
    m.question = "BTC up or down?"
    return m


def _wire(balance: float, tmp_path, monkeypatch):
    """Real DemoEngine + StrategyRunner (shared demo) wired into a TriggerEngine."""
    import trigger_engine
    from trigger_engine import TriggerConfig, TriggerEngine
    from strategy_runner import StrategyRunner
    from demo_engine import DemoEngine, DemoState

    # Isolate the trigger-positions persistence path (module-level constant) from other tests so a
    # position saved elsewhere can't leak in via _load_trigger_positions on construction.
    monkeypatch.setattr(
        trigger_engine, "_TRIGGER_POSITIONS_PATH", tmp_path / "trigger_positions.json", raising=False
    )

    demo = DemoEngine()
    demo.state = DemoState(balance_usd=balance)
    runner = StrategyRunner(demo)

    eng = TriggerEngine()
    eng._trigger_positions = {}  # start clean regardless of any pre-existing on-disk state
    eng.config = TriggerConfig(
        mode="signal", active=True, entry_price_cents=30.0,
        investment_usd=5.0, take_profit_pct=15.0,
    )
    eng.inject(demo, runner=runner)
    return eng, demo, runner


# ══════════════════════════════════════════════════════════════════════
#  1. Entry that exceeds 25% of balance is BLOCKED (no fill)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_entry_exceeding_25pct_of_balance_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    # balance $20 → 25% cap = $5. investment $10 @ ~0.20/contract → planned ~$9.8 > $5 cap,
    # yet < $20 balance so the existing insufficient-balance short-circuit does NOT catch it.
    eng, demo, runner = _wire(balance=20.0, tmp_path=tmp_path, monkeypatch=monkeypatch)
    runner.rt.config.circuit_breaker_enabled = False  # isolate the fraction guard
    eng.config.investment_usd = 10.0

    market = _mock_market()
    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=market)), \
         patch("market_discovery.seconds_until_window_end", new=MagicMock(return_value=200)):
        ok = await eng._execute_trade("Up", 0.20, "oversized entry")

    assert ok is False, "כניסה שחורגת מ-25% מהיתרה חייבת להיחסם"
    buys = [t for t in demo.state.trades if t.get("type") == "BUY"]
    assert buys == [], "אסור שיירשם BUY כשהמשמר חסם"
    assert market.token_up not in eng._trigger_positions
    assert eng.last_attempt_ts > 0  # backoff advanced (anti-spam)

    import fault_tracker
    rows = fault_tracker.list_faults()
    assert len(rows) == 1, f"ציפינו לתקלה אחת מנוכת-כפילויות, קיבלנו {len(rows)}"
    assert rows[0]["dedup_key"] == "entry_notional_exceeds_balance_fraction"


@pytest.mark.asyncio
async def test_oversized_entry_records_single_deduped_fault_over_many_ticks(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    eng, demo, runner = _wire(balance=20.0, tmp_path=tmp_path, monkeypatch=monkeypatch)
    runner.rt.config.circuit_breaker_enabled = False
    eng.config.investment_usd = 10.0

    market = _mock_market()
    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=market)), \
         patch("market_discovery.seconds_until_window_end", new=MagicMock(return_value=200)):
        for _ in range(6):
            await eng._execute_trade("Up", 0.20, "oversized entry")

    import fault_tracker
    rows = fault_tracker.list_faults()
    assert len(rows) == 1, f"ספאם: ציפינו לשורת-תקלה אחת, קיבלנו {len(rows)}"
    # one deduped row, count increments — not one row per attempt
    assert rows[0]["dedup_key"] == "entry_notional_exceeds_balance_fraction"


# ══════════════════════════════════════════════════════════════════════
#  2. Circuit-breaker tripped → Trigger does not enter
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_tripped_circuit_breaker_blocks_trigger_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    eng, demo, runner = _wire(balance=1000.0, tmp_path=tmp_path, monkeypatch=monkeypatch)  # ample balance — only the breaker should block
    # arm a genuine trip: 5 consecutive losses ≥ threshold of 3
    runner.rt.config.circuit_breaker_enabled = True
    runner.rt.config.circuit_breaker_max_consecutive_losses = 3
    demo.state.loss_recovery_streak = 5

    market = _mock_market()
    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=market)), \
         patch("market_discovery.seconds_until_window_end", new=MagicMock(return_value=200)):
        ok = await eng._execute_trade("Up", 0.20, "entry while breaker tripped")

    assert ok is False, "Circuit-breaker פעיל חייב לחסום כניסת טריגר"
    buys = [t for t in demo.state.trades if t.get("type") == "BUY"]
    assert buys == [], "אסור שיירשם BUY כשה-circuit-breaker חסם"
    assert market.token_up not in eng._trigger_positions
    assert runner.rt.circuit_breaker_tripped is True
    assert eng.last_attempt_ts > 0

    import fault_tracker
    rows = fault_tracker.list_faults()
    assert len(rows) == 1, f"ציפינו לתקלה אחת, קיבלנו {len(rows)}"
    assert rows[0]["dedup_key"] == "circuit_breaker_tripped"


# ══════════════════════════════════════════════════════════════════════
#  3. Within-limits entry still succeeds unchanged
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_within_limits_entry_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    eng, demo, runner = _wire(balance=1000.0, tmp_path=tmp_path, monkeypatch=monkeypatch)
    runner.rt.config.circuit_breaker_enabled = False
    eng.config.investment_usd = 5.0  # ~$5 planned << 25% of $1000 = $250

    market = _mock_market()
    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=market)), \
         patch("market_discovery.seconds_until_window_end", new=MagicMock(return_value=200)), \
         patch.object(demo, "best_ask", new=AsyncMock(return_value=0.20)):
        ok = await eng._execute_trade("Up", 0.20, "normal entry")

    assert ok is True
    buys = [t for t in demo.state.trades if t.get("type") == "BUY"]
    assert len(buys) == 1, "כניסה תקינה חייבת לרשום BUY אחד"
    assert eng.last_trigger_ts > 0
    assert eng.last_attempt_ts == 0.0  # guards did not touch a clean success
    assert market.token_up in eng._trigger_positions

    import fault_tracker
    assert fault_tracker.list_faults() == []
