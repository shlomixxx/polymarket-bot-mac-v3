"""בלמי-בטיחות נגד ה-blowup של 2026-06-15 (מכפיל 1525× → ניסיון $30k בכל טיק).

מכסה שלוש שכבות:
1. תקרת-ברזל מוחלטת על מכפיל שחזור-ההפסד (sizing + צבירה).
2. בלם יחסי-ליתרה: כניסה שה-notional שלה > 25% מהיתרה נחסמת (תקלה אחת מדודדפת).
3. round-trip של שדות-המגבלה החדשים בשמירה/טעינה של ה-config.
"""
from __future__ import annotations

import time

import pytest

from demo_engine import DemoEngine, DemoState
from loss_recovery import apply_loss_recovery_from_settlements
from strategy_runner import (
    HARD_MAX_LOSS_RECOVERY_MULT,
    MAX_ENTRY_FRACTION_OF_BALANCE,
    StrategyConfig,
    StrategyRunner,
)


def _runner(balance_usd: float = 1000.0) -> StrategyRunner:
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=balance_usd)
    return StrategyRunner(eng)


# ── (a) sizing נחסם ע"י המכפיל שהמשתמש הגדיר (config) — לא ע"י תקרת-ברזל קבועה של ×3 ──
def test_sizing_capped_by_user_config_multiplier():
    r = _runner()
    cfg = StrategyConfig(
        investment_usd=20.0,
        loss_recovery_enabled=True,
        loss_recovery_max_multiplier=50.0,  # המשתמש הגדיר תקרה 50×
    )
    r.demo.state.loss_recovery_multiplier = 1525.0  # גם אם ה-state התקלקל ל-1525×
    eff = r._effective_investment_usd(cfg)
    assert eff == pytest.approx(20.0 * 50.0)  # נחסם ל-config (50×), לא ל-1525× ולא ל-3×


def test_config_multiplier_above_3_is_honored():
    # התיקון: תקרת ×3 הישנה בוטלה — המכפיל שהמשתמש מגדיר נכבד (לא נחסם ל-3)
    r = _runner()
    cfg = StrategyConfig(investment_usd=20.0, loss_recovery_enabled=True,
                         loss_recovery_max_multiplier=20.0)
    r.demo.state.loss_recovery_multiplier = 20.0
    assert r._effective_investment_usd(cfg) == pytest.approx(20.0 * 20.0)  # 400, לא 60


def test_absolute_overflow_ceiling_still_bounds_insane_values():
    # תקרת-overflow מוחלטת (לא פונקציונלית) מונעת ערכים אבסורדיים/גלישה נומרית
    r = _runner()
    cfg = StrategyConfig(investment_usd=20.0, loss_recovery_enabled=True,
                         loss_recovery_max_multiplier=1e12)
    r.demo.state.loss_recovery_multiplier = 1e12
    assert r._effective_investment_usd(cfg) == pytest.approx(20.0 * HARD_MAX_LOSS_RECOVERY_MULT)


# ── (b) שחזור-הפסד כבוי → sizing = base בדיוק (התנהגות לא משתנה) ──
def test_disabled_loss_recovery_sizing_equals_base():
    r = _runner()
    cfg = StrategyConfig(investment_usd=20.0, loss_recovery_enabled=False)
    # גם אם המכפיל בסטייט גבוה — כבוי = base בלבד
    r.demo.state.loss_recovery_multiplier = 1525.0
    assert r._effective_investment_usd(cfg) == pytest.approx(20.0)


def test_multiplier_at_or_below_ceiling_is_unchanged():
    """כשהמכפיל ≤ 3 התנהגות ה-sizing זהה לחלוטין (אין רגרסיה)."""
    r = _runner()
    cfg = StrategyConfig(investment_usd=20.0, loss_recovery_enabled=True,
                         loss_recovery_max_multiplier=10.0)
    for m in (1.0, 1.2, 2.5, 3.0):
        r.demo.state.loss_recovery_multiplier = m
        assert r._effective_investment_usd(cfg) == pytest.approx(20.0 * m)


# ── (c) הצבירה נחסמת ב-config שהמשתמש הגדיר (לא ב-3 קבוע) ──
def test_climb_clamps_at_user_config_cap():
    st = DemoState(balance_usd=1000.0, loss_recovery_streak=0, loss_recovery_multiplier=1.0)
    for _ in range(50):
        apply_loss_recovery_from_settlements(
            st,
            enabled=True,
            step_pct=150.0,        # factor 2.5
            every_n_losses=1,
            max_multiplier=50.0,   # המשתמש הגדיר 50×
            settlement_trades=[{"realized_pnl": -10.0, "type": "SETTLE_LOSS"}],
        )
        assert st.loss_recovery_multiplier <= 50.0
    assert st.loss_recovery_multiplier == pytest.approx(50.0)  # מטפס עד ה-config, לא נחסם ב-3


# ── (d) בלם יחסי-ליתרה: כניסה גדולה מ-25% מהיתרה נחסמת, ונרשמת תקלה אחת מדודדפת ──
def test_balance_fraction_guard_blocks_oversized_entry():
    import fault_tracker

    r = _runner(balance_usd=7000.0)
    cfg = StrategyConfig(
        # מבטלים מגבלות אחרות כדי לבודד את בלם-היתרה
        max_trades_per_hour=0,
        max_entries_per_window=0,
        max_notional_per_window_usd=0.0,
        circuit_breaker_enabled=False,
    )
    r.rt.config = cfg
    now = time.time()

    # ניסיון להיכנס ל-$30k על יתרה של $7k (היחס 25% = $1750) → חסום
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=30000.0) is False

    # כניסה בתוך התקציב ($1000 < $1750) → מותר
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=1000.0) is True

    # בדיוק על הסף — לא מעל → מותר
    boundary = 7000.0 * MAX_ENTRY_FRACTION_OF_BALANCE
    assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=boundary) is True


def test_balance_fraction_guard_records_one_deduped_fault():
    import fault_tracker

    r = _runner(balance_usd=7000.0)
    cfg = StrategyConfig(
        max_trades_per_hour=0, max_entries_per_window=0,
        max_notional_per_window_usd=0.0, circuit_breaker_enabled=False,
    )
    r.rt.config = cfg
    now = time.time()

    def _count() -> int:
        rows = fault_tracker.list_faults(limit=5000)
        return sum(int(x.get("count") or 0)
                   for x in rows if x.get("dedup_key") == "entry_notional_exceeds_balance_fraction")

    before = _count()
    # 50 חסימות רצופות (כמו לולאת ה-incident) — חייב להיות dedup אחד, לא 50 שורות חדשות
    for _ in range(50):
        assert r._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=30000.0) is False
    after = _count()

    rows = fault_tracker.list_faults(limit=5000)
    keyed = [x for x in rows if x.get("dedup_key") == "entry_notional_exceeds_balance_fraction"]
    assert len(keyed) == 1  # שורה אחת בלבד — לא 50
    assert after - before == 50  # count עולה, אבל ב-row יחיד מדודדף


# ── (e) round-trip של שדות-המגבלה החדשים דרך save/load ──
def test_persisted_limit_fields_round_trip(tmp_path, monkeypatch):
    import main

    cfg_path = tmp_path / "config_persisted.json"
    monkeypatch.setattr(main, "CONFIG_PERSISTED_PATH", cfg_path)

    c = main.runner.rt.config
    # קובעים ערכים בטוחים שהמשתמש הגדיר — חייבים לשרוד restart
    saved = {
        "max_entries_per_window": 4,
        "max_notional_per_window_usd": 2500.0,
        "max_trades_per_hour": 250,
        "loss_recovery_max_multiplier": 2.0,
        "circuit_breaker_equity_floor_pct": 30.0,
        "circuit_breaker_enabled": True,
    }
    for k, v in saved.items():
        setattr(c, k, v)

    main._save_persisted_config()
    assert cfg_path.exists()

    # מאפסים בזיכרון לערכי-ברירת-מחדל "מסוכנים" כדי לוודא שהטעינה באמת מחזירה את השמורים
    c.max_entries_per_window = 99
    c.max_notional_per_window_usd = 50_000_000.0
    c.max_trades_per_hour = 100_000_000
    c.loss_recovery_max_multiplier = 100000.0
    c.circuit_breaker_equity_floor_pct = 0.0
    c.circuit_breaker_enabled = False

    main._load_persisted_config()

    for k, v in saved.items():
        assert getattr(c, k) == v, f"{k} did not round-trip (got {getattr(c, k)!r})"
