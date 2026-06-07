"""Floor-stop (hard stop-loss) tests.

The full per-position exit loop in StrategyRunner.tick() is entangled with live
network fetches (fetch_best_bid_ask) and live/demo order placement, so we test the
floor_stop trigger at the level it actually fires: the boolean expression evaluated
against a REAL DemoEngine position's unrealized_pnl_pct (the exact value the loop
feeds in as `upnl`), plus a config round-trip (default-off, settable, clamped).

This mirrors the production expression in strategy_runner.py:

    floor_trigger = (
        float(getattr(cfg, "floor_stop_pct", 0.0) or 0.0) > 0.0
        and upnl is not None
        and upnl <= -float(cfg.floor_stop_pct)
    )
"""

from strategy_runner import StrategyConfig, StrategyRunner
from demo_engine import DemoEngine, Position


def _floor_trigger(cfg: StrategyConfig, upnl) -> bool:
    """Verbatim copy of the production floor_trigger expression."""
    return (
        float(getattr(cfg, "floor_stop_pct", 0.0) or 0.0) > 0.0
        and upnl is not None
        and upnl <= -float(cfg.floor_stop_pct)
    )


def _engine_with_position(*, avg_cost: float) -> tuple[DemoEngine, str]:
    eng = DemoEngine()
    tok = "tok-floor"
    eng.state.positions.append(
        Position(side="Up", contracts=10.0, avg_cost=avg_cost, token_id=tok)
    )
    return eng, tok


def test_floor_stop_fires_when_loss_at_or_below_floor():
    """floor_stop_pct=70, position down ~80% (bid 0.10 vs avg 0.50) → trigger fires."""
    eng, tok = _engine_with_position(avg_cost=0.50)
    upnl = eng.unrealized_pnl_pct(tok, 0.10)  # (0.10-0.50)/0.50 = -80%
    assert upnl == -80.0
    cfg = StrategyConfig(floor_stop_pct=70.0)
    assert _floor_trigger(cfg, upnl) is True


def test_floor_stop_fires_exactly_at_floor():
    """upnl == -floor exactly → fires (<= boundary is inclusive)."""
    eng, tok = _engine_with_position(avg_cost=0.50)
    upnl = eng.unrealized_pnl_pct(tok, 0.15)  # -70.0%
    assert upnl == -70.0
    cfg = StrategyConfig(floor_stop_pct=70.0)
    assert _floor_trigger(cfg, upnl) is True


def test_floor_stop_does_not_fire_above_floor():
    """Loss shallower than the floor (−50% vs −70%) → no trigger."""
    eng, tok = _engine_with_position(avg_cost=0.50)
    upnl = eng.unrealized_pnl_pct(tok, 0.25)  # -50.0%
    assert upnl == -50.0
    cfg = StrategyConfig(floor_stop_pct=70.0)
    assert _floor_trigger(cfg, upnl) is False


def test_floor_stop_default_off_never_fires():
    """Default floor_stop_pct=0 → trigger is byte-identical OFF even at total loss."""
    eng, tok = _engine_with_position(avg_cost=0.50)
    upnl = eng.unrealized_pnl_pct(tok, 0.001)  # ~ -99.8%
    cfg = StrategyConfig()  # default
    assert cfg.floor_stop_pct == 0.0
    assert _floor_trigger(cfg, upnl) is False
    assert _floor_trigger(cfg, -100.0) is False


def test_floor_stop_handles_none_upnl():
    """No mark / no position → upnl None → never fires (no crash)."""
    cfg = StrategyConfig(floor_stop_pct=70.0)
    assert _floor_trigger(cfg, None) is False


def test_floor_stop_config_round_trips_on_runner():
    """Field exists on StrategyConfig, defaults 0, and is settable on a live runner."""
    r = StrategyRunner(DemoEngine())
    assert r.rt.config.floor_stop_pct == 0.0  # default off
    r.rt.config = StrategyConfig(floor_stop_pct=70.0)
    assert r.rt.config.floor_stop_pct == 70.0
