"""בדיקות פיצ'ר Follow Last Winner: get_last_window_winners + _resolve_follow_winner_side."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _fresh_tracker(tmp_dir: Path):
    """טעינה מחדש של history_tracker עם DATA_ROOT נקי, כדי לבודד את ה-DB בכל בדיקה."""
    os.environ["DATA_ROOT"] = str(tmp_dir)
    import importlib
    import history_tracker
    importlib.reload(history_tracker)
    return history_tracker


def test_get_last_window_winners_empty_db_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        assert ht.get_last_window_winners(window_sec=300, limit=5) == []


def test_get_last_window_winners_returns_recent_first():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        # 3 חלונות: epochs 100, 200, 300; הלייטסט (300) ראשון.
        ht.record_window_result(epoch=100, slug="s100", side_won="Up", btc_open=100.0, btc_close=101.0)
        ht.record_window_result(epoch=200, slug="s200", side_won="Down", btc_open=101.0, btc_close=100.5)
        ht.record_window_result(epoch=300, slug="s300", side_won="Up", btc_open=100.5, btc_close=102.0)
        out = ht.get_last_window_winners(window_sec=300, limit=2)
        assert len(out) == 2
        assert out[0]["epoch"] == 300
        assert out[0]["side_won"] == "Up"
        assert out[1]["epoch"] == 200
        assert out[1]["side_won"] == "Down"


def test_get_last_window_winners_filters_by_window_sec():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(epoch=100, slug="s5m", side_won="Up", btc_open=100.0, btc_close=101.0, window_sec=300)
        ht.record_window_result(epoch=200, slug="s15m", side_won="Down", btc_open=101.0, btc_close=99.0, window_sec=900)
        only5m = ht.get_last_window_winners(window_sec=300, limit=5)
        assert len(only5m) == 1 and only5m[0]["slug"] == "s5m"
        only15m = ht.get_last_window_winners(window_sec=900, limit=5)
        assert len(only15m) == 1 and only15m[0]["slug"] == "s15m"


def test_get_last_window_winners_min_drift_filter():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        # drift ~0.001% — מתחת לסף 0.01%
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100000.0, btc_close=100001.0)
        # drift ~0.05% — מעל הסף
        ht.record_window_result(epoch=200, slug="s2", side_won="Down", btc_open=100000.0, btc_close=99950.0)
        # drift ~0.1% — מעל הסף
        ht.record_window_result(epoch=300, slug="s3", side_won="Up", btc_open=100000.0, btc_close=100100.0)
        filtered = ht.get_last_window_winners(window_sec=300, limit=5, min_drift_pct=0.01)
        # רק 2 העברו את הסף
        assert len(filtered) == 2
        epochs = {r["epoch"] for r in filtered}
        assert epochs == {200, 300}


def test_get_last_window_winners_ignores_null_side_won():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(epoch=100, slug="s1", side_won=None, btc_open=100.0, btc_close=101.0)
        ht.record_window_result(epoch=200, slug="s2", side_won="Up", btc_open=101.0, btc_close=102.0)
        out = ht.get_last_window_winners(window_sec=300, limit=5)
        assert len(out) == 1 and out[0]["epoch"] == 200


def test_resolve_follow_winner_side_forward_single():
    """lookback=1, mode=forward, חלון אחרון Up → bot ייכנס Up."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        # יבוא נקי של המודולים אחרי שינוי DATA_ROOT
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        history_tracker.record_window_result(
            epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=102.0
        )
        import strategy_runner
        importlib.reload(strategy_runner)
        cfg = strategy_runner.StrategyConfig(
            follow_last_winner_enabled=True,
            follow_last_winner_lookback=1,
            follow_last_winner_mode="forward",
            follow_last_winner_min_btc_drift_pct=0.0,
            side_preference="Down",  # FLW אמור לעקוף את זה
        )
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        side = runner._resolve_follow_winner_side(cfg)
        assert side == "Up"


def test_resolve_follow_winner_side_reverse():
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        history_tracker.record_window_result(
            epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=102.0
        )
        import strategy_runner
        importlib.reload(strategy_runner)
        cfg = strategy_runner.StrategyConfig(
            follow_last_winner_enabled=True,
            follow_last_winner_lookback=1,
            follow_last_winner_mode="reverse",
        )
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        side = runner._resolve_follow_winner_side(cfg)
        assert side == "Down"  # reverse של Up


def test_resolve_follow_winner_side_majority_of_3():
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        # 2 Up, 1 Down — רוב Up
        history_tracker.record_window_result(epoch=100, slug="s1", side_won="Down", btc_open=100, btc_close=99)
        history_tracker.record_window_result(epoch=200, slug="s2", side_won="Up", btc_open=99, btc_close=100)
        history_tracker.record_window_result(epoch=300, slug="s3", side_won="Up", btc_open=100, btc_close=101)
        import strategy_runner
        importlib.reload(strategy_runner)
        cfg = strategy_runner.StrategyConfig(
            follow_last_winner_enabled=True,
            follow_last_winner_lookback=3,
        )
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        side = runner._resolve_follow_winner_side(cfg)
        assert side == "Up"


def test_resolve_follow_winner_side_tie_returns_none():
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        history_tracker.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100, btc_close=101)
        history_tracker.record_window_result(epoch=200, slug="s2", side_won="Down", btc_open=101, btc_close=100)
        import strategy_runner
        importlib.reload(strategy_runner)
        cfg = strategy_runner.StrategyConfig(
            follow_last_winner_enabled=True,
            follow_last_winner_lookback=2,
        )
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        side = runner._resolve_follow_winner_side(cfg)
        assert side is None  # תיקו → fallback


def test_resolve_follow_winner_side_no_history_returns_none():
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        import strategy_runner
        importlib.reload(strategy_runner)
        cfg = strategy_runner.StrategyConfig(
            follow_last_winner_enabled=True,
            follow_last_winner_lookback=1,
        )
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        side = runner._resolve_follow_winner_side(cfg)
        assert side is None  # אין history → fallback


def test_resolve_follow_winner_side_clamps_lookback():
    """lookback מחוץ ל-[1,5] מתוקן בתוך הפונקציה."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        for i in range(1, 11):
            history_tracker.record_window_result(
                epoch=i * 100, slug=f"s{i}", side_won="Up", btc_open=100, btc_close=101
            )
        import strategy_runner
        importlib.reload(strategy_runner)
        cfg = strategy_runner.StrategyConfig(
            follow_last_winner_enabled=True,
            follow_last_winner_lookback=100,  # מעבר לטווח
        )
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        side = runner._resolve_follow_winner_side(cfg)
        # 10 Up בהיסטוריה → גם clamp ל-5 → רוב Up
        assert side == "Up"


def test_resolve_follow_winner_side_routes_by_active_data_source():
    """FLW חייב לעקוב אחרי המנצח לפי ה-oracle של המקור הפעיל (Binance vs Polymarket/Chainlink),
    לא תמיד לפי Binance. הבדיקה זורעת שורה שבה שני ה-oracles חלוקים."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        # Binance side_won=Down, Polymarket(Chainlink) side_won=Up — המקורות חלוקים בכוונה.
        history_tracker.record_window_result(
            epoch=100, slug="s1", side_won="Down", side_won_polymarket="Up",
            btc_open=100.0, btc_close=99.0,
        )
        import strategy_runner
        importlib.reload(strategy_runner)
        import data_source
        cfg = strategy_runner.StrategyConfig(
            follow_last_winner_enabled=True,
            follow_last_winner_lookback=1,
            follow_last_winner_mode="forward",
        )
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        try:
            data_source.set_active("binance")
            assert runner._resolve_follow_winner_side(cfg) == "Down"
            data_source.set_active("polymarket")
            assert runner._resolve_follow_winner_side(cfg) == "Up"
        finally:
            data_source.set_active("polymarket")  # cleanup


def test_resolve_follow_winner_side_calls_get_last_window_winners_with_active_source():
    """Guardrail: מוודא שהקריאה עצמה מעבירה data_source (לא רק בודק תוצאה סופית)."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import history_tracker
        importlib.reload(history_tracker)
        import strategy_runner
        importlib.reload(strategy_runner)
        import data_source
        cfg = strategy_runner.StrategyConfig(follow_last_winner_enabled=True, follow_last_winner_lookback=1)
        from demo_engine import DemoEngine
        demo = DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=demo)
        runner.rt.config = cfg
        try:
            data_source.set_active("binance")
            with patch("history_tracker.get_last_window_winners", return_value=[]) as mocked:
                runner._resolve_follow_winner_side(cfg)
                assert mocked.call_args.kwargs.get("data_source") == "binance"
        finally:
            data_source.set_active("polymarket")  # cleanup
