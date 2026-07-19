"""
אנטי-ספאם ל-Trigger Engine: כניסה נדונה-לכישלון (אין יתרה מספקת) לא חוזרת בכל טיק.

רקע (התקלה): _execute_trade ניסה למלא דרך simulate_market_buy; ה-cooldown ב-_tick
מתבסס על last_trigger_ts שמתעדכן רק בהצלחה, לכן כניסה שנכשלת שוב ושוב מנסה כל טיק (~2ש׳)
ורושמת מאות אירועי 'error' זהים (462 אירועי 'אין יתרה מספקת' בריצה אחת, 0 שורות ב-faults.db).

התיקון (אנטי-ספאם בלבד — בלי תקרת 25% ובלי circuit breaker):
  (a) קיצור-דרך לפני המילוי כשעלות מתוכננת > יתרת דמו זמינה.
  (b) קידום חותמת backoff בכל כניסה שנכשלה/דולגה → ה-cooldown חוסם את הטיקים הבאים.
  (c) תקלה אחת מנוכת-כפילויות (dedup_key='trigger_insufficient_balance') במקום spam per-tick.
"""
import asyncio
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


def _fake_demo(balance: float):
    """דמו מזויף: קורא ל-simulate_market_buy מחזיר 'אין יתרה מספקת' (התנהגות הדמו האמיתי)."""
    demo = MagicMock()
    demo.state = MagicMock()
    demo.state.balance_usd = balance
    demo.state.positions = []
    demo.simulate_market_buy = AsyncMock(
        return_value={"ok": False, "error": "אין יתרה מספקת (נדרש ~4.81$)"}
    )
    return demo


# ══════════════════════════════════════════════════════════════════════
#  1. ONE deduped fault (count increments) + short-circuit לפני המילוי
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_insufficient_balance_records_single_deduped_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    from trigger_engine import TriggerConfig, TriggerEngine

    eng = TriggerEngine()
    eng.config = TriggerConfig(
        mode="signal", active=True, entry_price_cents=30.0, investment_usd=5.0
    )
    demo = _fake_demo(balance=0.10)  # יתרה זעירה — כל כניסה נדונה לכישלון
    eng.inject(demo)

    market = _mock_market()
    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=market)), \
         patch("market_discovery.seconds_until_window_end", new=MagicMock(return_value=200)):
        results = [await eng._execute_trade("Up", 0.20, "test") for _ in range(6)]

    # (a) short-circuit — לא ניסינו למלא בכלל
    assert all(r is False for r in results)
    demo.simulate_market_buy.assert_not_called()

    # (c) תקלה אחת בלבד, count עולה עם כל ניסיון
    import fault_tracker
    rows = fault_tracker.list_faults()
    assert len(rows) == 1, f"ציפינו לתקלה אחת מנוכת-כפילויות, קיבלנו {len(rows)}"
    assert rows[0]["dedup_key"] == "trigger_insufficient_balance"
    assert rows[0]["count"] == 6

    # (b) חותמת ה-backoff קודמה
    assert eng.last_attempt_ts > 0


# ══════════════════════════════════════════════════════════════════════
#  2. Backoff — לא מנסה למלא בכל טיק (respects cooldown)
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_tick_backs_off_and_does_not_retry_every_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    from trigger_engine import TriggerConfig, TriggerEngine

    eng = TriggerEngine()
    eng.config = TriggerConfig(
        mode="signal", active=True, signal_confidence=0.6,
        cooldown_sec=60.0, min_seconds_remaining=90,
        entry_price_cents=30.0, investment_usd=5.0,
    )
    demo = _fake_demo(balance=0.10)
    eng.inject(demo)

    # ולידציית חוזה עוברת בכל טיק כך שהסיגנל "יורה" שוב ושוב
    eng._get_window_info = AsyncMock(return_value=(200, 300))
    eng._fetch_contract_ask = AsyncMock(return_value=0.20)

    market = _mock_market()
    sig = {"recommendation": "Up", "up_confidence": 0.95, "down_confidence": 0.05}

    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=market)), \
         patch("market_discovery.seconds_until_window_end", new=MagicMock(return_value=200)), \
         patch("signal_engine.compute_signals", new=AsyncMock(return_value=sig)):
        for _ in range(5):
            await eng._tick()

    # short-circuit: אף פעם לא ניסינו למלא
    demo.simulate_market_buy.assert_not_called()

    # backoff: על אף 5 טיקים — ניסינו כניסה פעם אחת בלבד (אחרת count היה 5)
    import fault_tracker
    rows = fault_tracker.list_faults()
    assert len(rows) == 1
    assert rows[0]["count"] == 1, (
        f"ה-backoff נשבר: ניסינו {rows[0]['count']} פעמים ב-5 טיקים במקום פעם אחת"
    )


# ══════════════════════════════════════════════════════════════════════
#  3. כניסה מוצלחת עם יתרה מספקת — עדיין עובדת ללא שינוי
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_successful_entry_with_sufficient_balance_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    from trigger_engine import TriggerConfig, TriggerEngine
    from demo_engine import DemoEngine, DemoState

    eng = TriggerEngine()
    eng.config = TriggerConfig(
        mode="signal", active=True, entry_price_cents=30.0,
        investment_usd=5.0, take_profit_pct=15.0,
    )
    demo = DemoEngine()
    demo.state = DemoState(balance_usd=1000.0)
    eng.inject(demo)

    market = _mock_market()
    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=market)), \
         patch("market_discovery.seconds_until_window_end", new=MagicMock(return_value=200)), \
         patch.object(demo, "best_ask", new=AsyncMock(return_value=0.20)):
        ok = await eng._execute_trade("Up", 0.20, "normal entry")

    assert ok is True
    buys = [t for t in demo.state.trades if t.get("type") == "BUY"]
    assert len(buys) == 1, "כניסה מוצלחת חייבת לרשום BUY אחד"
    assert eng.last_trigger_ts > 0             # הצלחה מקדמת את חותמת הטריגר
    assert eng.last_attempt_ts == 0.0          # backoff לא נגע בהצלחה
    assert market.token_up in eng._trigger_positions

    import fault_tracker
    assert fault_tracker.list_faults() == []   # אין תקלה על הצלחה
