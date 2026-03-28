import time

from strategy_runner import (
    StrategyConfig,
    StrategyRuntime,
    StrategyRunner,
    contracts_from_investment,
    dca_ref_price_from_ask,
)
from demo_engine import DemoEngine
from pricing_limits import MAX_LEGIT_SHARE_PRICE_USD, MIN_LEGIT_SHARE_PRICE_USD


def test_contracts_from_investment_rejects_near_zero_price():
    assert contracts_from_investment(100.0, MIN_LEGIT_SHARE_PRICE_USD * 0.5, 5) == 0  # מתחת ל־0.01
    assert contracts_from_investment(1.0, 0.01, 5) == int(1.0 // 0.01)  # 100
    assert contracts_from_investment(1.0, 1.0, 5) == 0  # מעל 0.99
    assert contracts_from_investment(10.0, MAX_LEGIT_SHARE_PRICE_USD, 5) == int(10.0 // MAX_LEGIT_SHARE_PRICE_USD)


def test_time_gates_freeze_and_intermediate_logic():
    r = StrategyRunner(DemoEngine())
    cfg = StrategyConfig(freeze_last_minutes=1.0, min_minutes_for_entry=3.0, intermediate_block_new_entries=True)

    assert r._time_gates(0.5, cfg) == "freeze"
    assert r._time_gates(1.5, cfg) == "intermediate"
    assert r._time_gates(3.5, cfg) == "ok"

    cfg2 = StrategyConfig(freeze_last_minutes=1.0, min_minutes_for_entry=3.0, intermediate_block_new_entries=False)
    assert r._time_gates(1.5, cfg2) == "ok"


def test_status_key_throttles_log_spam():
    rt = StrategyRuntime()
    rt.log_lines = []

    # סטטוס ראשון: נכתב ליומן
    rt.status("סטטוס: דקה אחרונה (0.60 דק׳)", key="freeze", repeat_interval_sec=5.0)
    assert len(rt.log_lines) == 1

    # אותו key עם טקסט שונה (המספר משתנה) — לא אמור להיכתב מייד
    rt.status("סטטוס: דקה אחרונה (0.59 דק׳)", key="freeze", repeat_interval_sec=5.0)
    assert len(rt.log_lines) == 1

    # אחרי שעבר repeat_interval, יכתב שוב
    rt._last_status_ts -= 6.0
    rt.status("סטטוס: דקה אחרונה (0.58 דק׳)", key="freeze", repeat_interval_sec=5.0)
    assert len(rt.log_lines) == 2


def test_entry_limits_cooldown_and_limits():
    eng = DemoEngine()
    r = StrategyRunner(eng)
    cfg = StrategyConfig(
        max_trades_per_hour=2,
        max_entries_per_window=1,
        max_notional_per_window_usd=10.0,
        reenter_cooldown_sec=8.0,
        auto_reenter_after_tp=True,
    )
    r.rt.config = cfg
    now = time.time()

    # trade/hour: שתי עסקאות קיימות חוסמות
    r.rt.trade_timestamps = [now - 10, now - 20]
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1.0) is False


def test_cooldown_allows_switch_side_in_hedge_mode():
    eng = DemoEngine()
    r = StrategyRunner(eng)
    now = time.time()

    r.rt.last_tp_ts = now - 1.0
    r.rt.last_tp_side = "Down"

    cfg = StrategyConfig(
        hedge_enabled=True,
        reenter_cooldown_sec=8.0,
    )
    assert r._cooldown_allows_reentry(now=now, cfg=cfg, planned_side="Up") is True
    assert r._cooldown_allows_reentry(now=now, cfg=cfg, planned_side="Down") is False


def test_cooldown_blocks_in_non_hedge_mode():
    eng = DemoEngine()
    r = StrategyRunner(eng)
    now = time.time()

    r.rt.last_tp_ts = now - 1.0
    r.rt.last_tp_side = "Down"

    cfg = StrategyConfig(
        hedge_enabled=False,
        reenter_cooldown_sec=8.0,
    )
    assert r._cooldown_allows_reentry(now=now, cfg=cfg, planned_side="Up") is False


def test_record_tp_resets_hedge_leg2_done():
    eng = DemoEngine()
    r = StrategyRunner(eng)
    r.rt.hedge_leg2_done = True

    cfg = StrategyConfig(hedge_enabled=True)
    r.rt.record_tp(cfg=cfg, side="Down")

    assert r.rt.hedge_leg2_done is False


def test_record_tp_resets_dca_counters_when_enabled():
    eng = DemoEngine()
    r = StrategyRunner(eng)
    r.rt.dca_done_slices = 2
    r.rt.last_dca_ts = time.time() - 10

    cfg = StrategyConfig(dca_enabled=True, auto_reenter_after_tp=True)
    r.rt.record_tp(cfg=cfg, side="Up")

    assert r.rt.dca_done_slices == 0
    assert r.rt.last_dca_ts == 0.0


def test_dca_ref_price_from_ask_discount_enabled():
    cfg = StrategyConfig(dca_discount_enabled=True, dca_discount_pct=2.0)
    # 2% הנחה: 0.50 -> 0.49 (אין cap במקרה הזה)
    ref = dca_ref_price_from_ask(ask=0.50, entry_target_usd=0.50, cfg=cfg)
    assert ref == 0.49


def test_dca_ref_price_from_ask_discount_capped_by_entry_target():
    cfg = StrategyConfig(dca_discount_enabled=True, dca_discount_pct=1.0)
    # ref=0.6*(1-0.01)=0.594, אבל cap=entry*1.05=0.525 => cap wins
    ref = dca_ref_price_from_ask(ask=0.60, entry_target_usd=0.50, cfg=cfg)
    assert ref == 0.525


