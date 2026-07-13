"""בדיקות ניתוב היסטוריית חלונות לפי data_source (Binance vs Polymarket/Chainlink).

מכסה את התיקון: window_results מקבל עמודה נוספת side_won_polymarket, ו-
get_recent_windows/get_last_window_winners/get_hourly_breakdown מקבלים פרמטר
data_source שמחליט איזו עמודה "מנצחת" בתשובה (side_won נשאר תמיד שם המפתח).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _fresh_tracker(tmp_dir: Path):
    """טעינה מחדש של history_tracker עם DATA_ROOT נקי, לבידוד ה-DB."""
    os.environ["DATA_ROOT"] = str(tmp_dir)
    import importlib
    import history_tracker
    importlib.reload(history_tracker)
    return history_tracker


# ── get_recent_windows ───────────────────────────────────────────────────────

def test_get_recent_windows_binance_source_uses_side_won():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(
            epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
            btc_open=100.0, btc_close=101.0,
        )
        rows = ht.get_recent_windows(window_sec=300, data_source="binance")
        assert len(rows) == 1
        assert rows[0]["side_won"] == "Up"


def test_get_recent_windows_polymarket_source_uses_side_won_polymarket():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(
            epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
            btc_open=100.0, btc_close=101.0,
        )
        rows = ht.get_recent_windows(window_sec=300, data_source="polymarket")
        assert len(rows) == 1
        assert rows[0]["side_won"] == "Down"


def test_get_recent_windows_polymarket_falls_back_when_null():
    """שורה ישנה בלי side_won_polymarket → מצב polymarket נופל ל-side_won (Binance)."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=101.0)
        rows = ht.get_recent_windows(window_sec=300, data_source="polymarket")
        assert rows[0]["side_won"] == "Up"


def test_get_recent_windows_default_is_binance():
    """ברירת המחדל (בלי data_source) חייבת להתנהג כמו binance — לא לשבור קוד קיים."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(
            epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
            btc_open=100.0, btc_close=101.0,
        )
        rows = ht.get_recent_windows(window_sec=300)
        assert rows[0]["side_won"] == "Up"


# ── get_last_window_winners ──────────────────────────────────────────────────

def test_get_last_window_winners_binance_source_uses_side_won():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(
            epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
            btc_open=100.0, btc_close=101.0,
        )
        rows = ht.get_last_window_winners(window_sec=300, limit=1, data_source="binance")
        assert rows[0]["side_won"] == "Up"


def test_get_last_window_winners_polymarket_source_uses_side_won_polymarket():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(
            epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
            btc_open=100.0, btc_close=101.0,
        )
        rows = ht.get_last_window_winners(window_sec=300, limit=1, data_source="polymarket")
        assert rows[0]["side_won"] == "Down"


def test_get_last_window_winners_polymarket_falls_back_when_null():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=101.0)
        rows = ht.get_last_window_winners(window_sec=300, limit=1, data_source="polymarket")
        assert rows[0]["side_won"] == "Up"


def test_get_last_window_winners_mixed_rows_per_source():
    """כמה חלונות, חלקם עם polymarket outcome שונה — כל source מחזיר את הרצף שלו."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
                                 btc_open=100.0, btc_close=101.0)
        ht.record_window_result(epoch=400, slug="s2", side_won="Down", side_won_polymarket="Down",
                                 btc_open=101.0, btc_close=99.0)
        binance_rows = ht.get_last_window_winners(window_sec=300, limit=2, data_source="binance")
        polymarket_rows = ht.get_last_window_winners(window_sec=300, limit=2, data_source="polymarket")
        assert [r["side_won"] for r in binance_rows] == ["Down", "Up"]
        assert [r["side_won"] for r in polymarket_rows] == ["Down", "Down"]


# ── get_hourly_breakdown ──────────────────────────────────────────────────────

def test_get_hourly_breakdown_routes_by_source():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        # epoch 100 → hour_utc 0 (1970-01-01T00:01:40Z)
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
                                 btc_open=100.0, btc_close=101.0)
        binance_hourly = ht.get_hourly_breakdown(window_sec=300, data_source="binance")
        polymarket_hourly = ht.get_hourly_breakdown(window_sec=300, data_source="polymarket")
        assert binance_hourly[0]["up_wins"] == 1
        assert polymarket_hourly[0]["up_wins"] == 0


# ── record_window_result persists side_won_polymarket ────────────────────────

def test_record_window_result_persists_side_won_polymarket():
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht.record_window_result(
            epoch=100, slug="s1", side_won="Up", side_won_polymarket="Down",
            btc_open=100.0, btc_close=101.0,
        )
        conn = ht._get_conn()
        row = conn.execute(
            "SELECT side_won, side_won_polymarket FROM window_results WHERE epoch=?", (100,)
        ).fetchone()
        assert row["side_won"] == "Up"
        assert row["side_won_polymarket"] == "Down"


# ── migration idempotency ────────────────────────────────────────────────────

def test_migration_adds_column_to_pre_existing_db_without_crashing():
    """DB ישן (לפני התיקון) בלי עמודת side_won_polymarket — האתחול חייב להוסיף אותה בלי קריסה."""
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "history.db"
        # יוצרים DB "ישן" ידנית — בדיוק הסכימה שהייתה לפני התיקון (בלי side_won_polymarket).
        raw = sqlite3.connect(str(db_path))
        raw.execute(
            """
            CREATE TABLE window_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch INTEGER NOT NULL,
                slug TEXT NOT NULL,
                window_sec INTEGER NOT NULL DEFAULT 300,
                side_won TEXT,
                btc_open REAL,
                btc_close REAL,
                ts_recorded REAL NOT NULL,
                hour_utc INTEGER,
                weekday INTEGER,
                UNIQUE(epoch, slug)
            )
            """
        )
        raw.execute(
            "INSERT INTO window_results (epoch, slug, window_sec, side_won, ts_recorded, hour_utc, weekday) "
            "VALUES (100, 's1', 300, 'Up', 1.0, 0, 0)"
        )
        raw.commit()
        raw.close()

        ht = _fresh_tracker(Path(d))
        # אתחול ראשון — לא אמור לקרוס, ואמור להוסיף את העמודה.
        conn = ht._get_conn()
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(window_results)").fetchall()]
        assert "side_won_polymarket" in cols
        # השורה הישנה שרדה, side_won_polymarket שלה NULL → fallback ל-side_won ב-polymarket mode.
        rows = ht.get_last_window_winners(window_sec=300, limit=1, data_source="polymarket")
        assert rows[0]["side_won"] == "Up"

        # קריאה שנייה לאתחול (למשל reload נוסף) — גם היא לא אמורה לקרוס (idempotent).
        import importlib
        importlib.reload(ht)
        conn2 = ht._get_conn()
        cols2 = [r["name"] for r in conn2.execute("PRAGMA table_info(window_results)").fetchall()]
        assert "side_won_polymarket" in cols2


def test_init_twice_on_existing_db_does_not_crash():
    """קריאה כפולה ל-_get_conn (שמריצה את מיגרציית ה-ALTER) לא אמורה לקרוס בפעם השנייה."""
    with tempfile.TemporaryDirectory() as d:
        ht = _fresh_tracker(Path(d))
        ht._get_conn()
        ht._get_conn()  # קריאה שנייה — ALTER TABLE לא אמור לרוץ שוב / לא אמור לקרוס
        ht.record_window_result(epoch=100, slug="s1", side_won="Up", btc_open=100.0, btc_close=101.0)
        assert ht.get_recent_windows(window_sec=300)[0]["side_won"] == "Up"
