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
    """enabled + streak >= max_consecutive_losses → entries blocked, tripped flag set,
    and a cooldown is armed (self-recovering: it will auto-resume after the cooldown)."""
    r, cfg = _runner_with_streak(enabled=True, streak=3, max_losses=3)
    now = time.time()

    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is False
    assert r.rt.circuit_breaker_tripped is True
    assert r.rt.circuit_breaker_reason  # non-empty reason
    assert r.rt.circuit_breaker_cooldown_until > now  # cooldown armed


def test_circuit_breaker_default_off_does_not_block():
    """Default-off: identical streak does NOT block, tripped flag stays False."""
    r, cfg = _runner_with_streak(enabled=False, streak=3, max_losses=3)
    now = time.time()

    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is True
    assert r.rt.circuit_breaker_tripped is False


def test_circuit_breaker_default_off_ignores_streak_regardless():
    """Default-off must be byte-identical behavior: even a huge streak → entries allowed."""
    r, cfg = _runner_with_streak(enabled=False, streak=9999, max_losses=3)
    now = time.time()

    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is True
    assert r.rt.circuit_breaker_tripped is False
    assert r.rt.circuit_breaker_cooldown_until == 0.0  # never armed


def test_circuit_breaker_stays_blocked_during_cooldown_without_reevaluating():
    """While in cooldown the breaker keeps blocking WITHOUT re-evaluating: even if the
    streak is cleared to 0, _entry_limits_ok still returns False until the cooldown ends."""
    r, cfg = _runner_with_streak(enabled=True, streak=3, max_losses=3)
    now = time.time()

    # First call trips it and arms the cooldown.
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is False
    cooldown_until = r.rt.circuit_breaker_cooldown_until
    assert cooldown_until > now

    # Clear the streak — a fresh evaluation WOULD allow entries — but cooldown must override.
    r.demo.state.loss_recovery_streak = 0
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is False
    assert r.rt.circuit_breaker_tripped is True
    # Cooldown deadline must be unchanged (no re-arming / re-recording during cooldown).
    assert r.rt.circuit_breaker_cooldown_until == cooldown_until


def test_circuit_breaker_self_recovers_after_cooldown():
    """After the cooldown expires the breaker auto-resumes: streak is reset to 0, the
    tripped flag clears, and entries are allowed again — no permanent deadlock."""
    r, cfg = _runner_with_streak(enabled=True, streak=3, max_losses=3)
    now = time.time()

    # Trip it.
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is False
    assert r.rt.circuit_breaker_tripped is True

    # Simulate the cooldown having elapsed.
    r.rt.circuit_breaker_cooldown_until = now - 1.0

    # Now it self-recovers: streak reset, flag cleared, entries allowed.
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is True
    assert r.rt.circuit_breaker_tripped is False
    assert r.demo.state.loss_recovery_streak == 0
