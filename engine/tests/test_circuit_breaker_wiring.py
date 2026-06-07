import time

from strategy_runner import StrategyConfig, StrategyRunner
from demo_engine import DemoEngine


def _runner_with_streak(*, enabled: bool, streak: int, max_losses: int):
    """Build a StrategyRunner + DemoEngine (mirrors test_strategy_runtime.py setup)
    with a loss-recovery streak primed and the circuit-breaker config applied."""
    eng = DemoEngine()
    eng.state.loss_recovery_streak = streak
    r = StrategyRunner(eng)
    cfg = StrategyConfig(
        circuit_breaker_enabled=enabled,
        circuit_breaker_max_consecutive_losses=max_losses,
    )
    r.rt.config = cfg
    return r, cfg


def test_circuit_breaker_blocks_entries_when_enabled_and_streak_hit():
    """enabled + streak >= max_consecutive_losses → entries blocked, tripped flag set."""
    r, cfg = _runner_with_streak(enabled=True, streak=3, max_losses=3)
    now = time.time()

    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is False
    assert r.rt.circuit_breaker_tripped is True
    assert r.rt.circuit_breaker_reason  # non-empty reason


def test_circuit_breaker_default_off_does_not_block():
    """Default-off: identical streak does NOT block, tripped flag stays False."""
    r, cfg = _runner_with_streak(enabled=False, streak=3, max_losses=3)
    now = time.time()

    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is True
    assert r.rt.circuit_breaker_tripped is False
