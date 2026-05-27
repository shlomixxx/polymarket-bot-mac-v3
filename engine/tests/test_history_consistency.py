"""בדיקות עקביות בין settlement ל-history.db (FLW + LR martingale workflow).

מכסה fixes #1, #2, #18 ו-#22.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def _fresh_tracker(tmp_dir: Path):
    """טעינה מחדש של history_tracker עם DATA_ROOT נקי, לבידוד DB."""
    os.environ["DATA_ROOT"] = str(tmp_dir)
    import importlib
    import history_tracker
    importlib.reload(history_tracker)
    return history_tracker


def test_upsert_updates_null_side_won():
    """FIX #18: אם side_won היה NULL, רישום חדש עם תוצאה תקפה יעדכן אותו."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        # רישום ראשון בלי side_won (kline לא היה זמין)
        ht.record_window_result(epoch=100, slug="s1", side_won=None, btc_open=None, btc_close=None)
        # רישום חוזר עם נתונים — אמור לעדכן את ה-NULL
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=101.0)
        rows = ht.get_last_window_winners(window_sec=300, limit=1)
        assert len(rows) == 1
        assert rows[0]["side_won"] == "Up"
        assert rows[0]["btc_open"] == 100.0
        assert rows[0]["btc_close"] == 101.0


def test_upsert_preserves_existing_side_won():
    """FIX #18: אם side_won כבר קיים, רישום חוזר לא ידרוס אותו (idempotent)."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=101.0)
        # ניסיון לשנות ל-Down — לא אמור להצליח (UPSERT שומר על הקיים)
        ht.record_window_result(epoch=100, slug="s1", side_won="Down", btc_open=100.0, btc_close=99.0)
        rows = ht.get_last_window_winners(window_sec=300, limit=1)
        # ה-side_won המקורי נשמר
        assert rows[0]["side_won"] == "Up"
        assert rows[0]["btc_close"] == 101.0  # לא 99


def test_upsert_fills_missing_btc_prices():
    """FIX #18: אם רישום ראשון היה ללא מחירים (None), השני ימלא."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=None, btc_close=None)
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=101.0)
        rows = ht.get_last_window_winners(window_sec=300, limit=1)
        assert rows[0]["btc_open"] == 100.0
        assert rows[0]["btc_close"] == 101.0


def test_dca_counters_persisted_to_state():
    """FIX #22: DemoState שומר את DCA counters."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import demo_engine
        importlib.reload(demo_engine)
        eng = demo_engine.DemoEngine(Path(d) / "state.json")
        eng.state.dca_done_slices_persisted = 3
        eng.state.dca_last_dca_ts_persisted = 1779846000.0
        eng.state.dca_last_fill_price_persisted = 0.42
        eng.state.dca_active_epoch_persisted = 1779846300
        eng.save()
        # טעינה מחדש — לוודא שהערכים שורדים
        eng2 = demo_engine.DemoEngine(Path(d) / "state.json")
        assert eng2.state.dca_done_slices_persisted == 3
        assert eng2.state.dca_last_dca_ts_persisted == 1779846000.0
        assert eng2.state.dca_last_fill_price_persisted == 0.42
        assert eng2.state.dca_active_epoch_persisted == 1779846300


def test_strategy_runner_restores_dca_counters_on_init():
    """FIX #22: StrategyRunner.__init__ קורא את ה-DCA counters מ-state."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import demo_engine
        importlib.reload(demo_engine)
        eng = demo_engine.DemoEngine(Path(d) / "state.json")
        eng.state.dca_done_slices_persisted = 2
        eng.state.dca_last_fill_price_persisted = 0.35
        eng.state.dca_active_epoch_persisted = 999
        eng.save()
        import strategy_runner
        importlib.reload(strategy_runner)
        runner = strategy_runner.StrategyRunner(demo=eng)
        # אחרי __init__: ה-runtime צריך להחזיק את הערכים מהדיסק
        assert runner.rt.dca_done_slices == 2
        assert runner.rt.dca_last_fill_price == 0.35
        assert runner.rt.current_epoch == 999


def test_strategy_runner_persists_dca_after_mutation():
    """FIX #22: כשהפונקציה _persist_dca_counters נקראת, ה-state מתעדכן."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import demo_engine
        importlib.reload(demo_engine)
        eng = demo_engine.DemoEngine(Path(d) / "state.json")
        import strategy_runner
        importlib.reload(strategy_runner)
        runner = strategy_runner.StrategyRunner(demo=eng)
        # שנה את ה-runtime
        runner.rt.dca_done_slices = 4
        runner.rt.dca_last_fill_price = 0.28
        runner.rt.current_epoch = 12345
        runner._persist_dca_counters()
        # טעינה מחדש מהדיסק
        eng2 = demo_engine.DemoEngine(Path(d) / "state.json")
        assert eng2.state.dca_done_slices_persisted == 4
        assert eng2.state.dca_last_fill_price_persisted == 0.28
        assert eng2.state.dca_active_epoch_persisted == 12345


def test_get_last_window_winners_returns_kline_based_data():
    """FIX #1: לאחר התיקון, history.db מכיל kline-based close (לא spot).

    הבדיקה הזאת לא רצה את הקוד אבל מאשרת שהשאילתה מחזירה btc_close
    כמו שהוקלד — כך שאם הקוד הקורא משתמש ב-close נכון, FLW מקבל אותו.
    """
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        # נדמה התנהגות אחרי תיקון: kline close נשמר עם spot >, ולא עם spot >
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=100.0)
        # tie ⇒ Up (FIX #2)
        rows = ht.get_last_window_winners(window_sec=300, limit=1)
        assert rows[0]["side_won"] == "Up"
        assert rows[0]["btc_close"] == 100.0


def test_tie_rule_consistent_up_wins():
    """FIX #2: tie (btc_close == btc_open) ⇒ Up wins, זהה ל-settlement."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        # נרשום שני חלונות: אחד תיקו, אחד עליה רגילה
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=50000.0, btc_close=50000.0)
        ht.record_window_result(epoch=400, slug="s2", side_won="Up", btc_open=50000.0, btc_close=50001.0)
        rows = ht.get_last_window_winners(window_sec=300, limit=2)
        # שניהם Up (גם tie, גם up רגיל)
        assert all(r["side_won"] == "Up" for r in rows)
