import time
from unittest.mock import AsyncMock, patch

import pytest

from strategy_runner import (
    StrategyConfig,
    StrategyRuntime,
    StrategyRunner,
    contracts_from_investment,
    dca_ref_price_from_ask,
    effective_price_for_contract_qty,
    entry_limit_price,
    market_entry_price_too_high,
)
from demo_engine import DemoEngine, DemoState
from pricing_limits import MAX_LEGIT_SHARE_PRICE_USD, MIN_LEGIT_SHARE_PRICE_USD


def test_contracts_from_investment_rejects_near_zero_price():
    assert contracts_from_investment(100.0, MIN_LEGIT_SHARE_PRICE_USD * 0.5, 5) == 0  # מתחת ל־0.01
    assert contracts_from_investment(1.0, 0.01, 5) == int(1.0 // 0.01)  # 100
    assert contracts_from_investment(1.0, 1.0, 5) == 0  # מעל 0.99
    assert contracts_from_investment(10.0, MAX_LEGIT_SHARE_PRICE_USD, 5) == int(10.0 // MAX_LEGIT_SHARE_PRICE_USD)


def test_effective_price_for_contract_qty_uses_lower_ask():
    """כש-Ask נמוך מתקרת entry — כמות חוזים לפי Ask (תקציב / מחיר בפועל)."""
    assert effective_price_for_contract_qty(0.50, 0.11) == pytest.approx(0.11)
    assert effective_price_for_contract_qty(0.50, 0.60) == pytest.approx(0.50)
    assert effective_price_for_contract_qty(0.50, None) == pytest.approx(0.50)


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


# ── entry_limit_price: "כניסה בכל חלון" (market) מול משמעת מחיר (limit) ──────────

def test_entry_limit_price_limit_mode_clamps_to_cap():
    """מצב limit: ה-limit לא עובר את ה-cap, גם אם ה-Ask גבוה ממנו (התנהגות קודמת)."""
    # Ask 0.55 מעל cap 0.51 → נחסם ל-0.51 (עלול לא להתמלא ולדלג על החלון)
    assert entry_limit_price(0.55, 0.51, order_mode="limit") == pytest.approx(0.51)
    # Ask 0.30 מתחת ל-cap 0.51 → ask*1.01 (marketable מתחת ל-cap)
    assert entry_limit_price(0.30, 0.51, order_mode="limit") == pytest.approx(0.303)


def test_entry_limit_price_market_mode_is_marketable_above_cap():
    """מצב market: ה-limit עוקב אחרי ה-Ask (+סליפג׳) ולא נחסם ל-cap — כך נכנסים לכל חלון."""
    lim = entry_limit_price(0.55, 0.51, order_mode="market", entry_slippage_pct=2.0)
    assert lim == pytest.approx(0.55 * 1.02)  # 0.561 — מעל ה-Ask → מתמלא מיד
    assert lim >= 0.55  # marketable: לא נחסם ל-cap 0.51


def test_entry_limit_price_market_mode_capped_at_legit_ceiling():
    """מצב market: גם עם Ask גבוה+סליפג׳ גדול, ה-limit לא עובר את תקרת המחיר החוקית (0.99)."""
    lim = entry_limit_price(0.98, 0.51, order_mode="market", entry_slippage_pct=50.0)
    assert lim == pytest.approx(MAX_LEGIT_SHARE_PRICE_USD)


@pytest.mark.asyncio
async def test_market_mode_fills_when_ask_above_cap_but_limit_mode_skips(tmp_path):
    """שחזור הבאג + התיקון ברמת מנוע הדמו:

    כש-Ask (0.55) מעל ה-cap (entry_price_cents=51 → 0.51):
    - מצב limit: lim נחסם ל-0.51 → simulate_market_buy דוחה (Ask מעל הלימיט) → דילוג על החלון.
    - מצב market: lim = ask*(1+סליפג׳) = 0.561 → simulate_market_buy מתמלא → כניסה לחלון.
    """
    ask = 0.55
    cap = 0.51  # entry_price_cents=51
    ctx = {"order_min_size": 5}

    # --- מצב limit: דילוג (הבאג) ---
    eng_limit = DemoEngine(state_path=tmp_path / "limit.json")
    eng_limit.state = DemoState(balance_usd=1000.0)
    lim_limit = entry_limit_price(ask, cap, order_mode="limit")
    assert lim_limit == pytest.approx(0.51)
    with patch.object(eng_limit, "best_ask", AsyncMock(return_value=ask)):
        r_limit = await eng_limit.simulate_market_buy(
            "Up", "tok", 10.0, limit_price=lim_limit, context=ctx
        )
    assert r_limit["ok"] is False  # נדחה — החלון מדולג

    # --- מצב market: כניסה (התיקון) ---
    eng_market = DemoEngine(state_path=tmp_path / "market.json")
    eng_market.state = DemoState(balance_usd=1000.0)
    lim_market = entry_limit_price(ask, cap, order_mode="market", entry_slippage_pct=2.0)
    assert lim_market >= ask
    with patch.object(eng_market, "best_ask", AsyncMock(return_value=ask)):
        r_market = await eng_market.simulate_market_buy(
            "Up", "tok", 10.0, limit_price=lim_market, context=ctx
        )
    assert r_market["ok"] is True  # נכנס לחלון
    assert r_market["trade"]["price"] == pytest.approx(ask)  # מילוי במחיר השוק


# ── market_entry_price_too_high: תקרת מחיר שפויה למצב market ─────────────────────

def test_market_price_cap_blocks_only_above_cap_in_market_mode():
    # תקרה 80¢: צד ב-0.85 → מדלגים; צד ב-0.75 → נכנסים
    assert market_entry_price_too_high(0.85, "market", 80.0) is True
    assert market_entry_price_too_high(0.75, "market", 80.0) is False
    # בדיוק על התקרה (0.80) — לא מעל → לא מדלגים
    assert market_entry_price_too_high(0.80, "market", 80.0) is False


def test_market_price_cap_disabled_values_never_block():
    # 0 או 100 = ללא תקרה (כניסה בכל מחיר), גם ב-0.99
    assert market_entry_price_too_high(0.99, "market", 0.0) is False
    assert market_entry_price_too_high(0.99, "market", 100.0) is False


def test_market_price_cap_ignored_in_limit_mode():
    # במצב limit התקרה לא רלוונטית (entry_price_cents הוא ה-cap)
    assert market_entry_price_too_high(0.95, "limit", 80.0) is False


def test_market_price_cap_handles_missing_ask():
    assert market_entry_price_too_high(None, "market", 80.0) is False


