"""
מסד נתוני תוצאות חלונות BTC Up/Down (SQLite).
עוקב: מי ניצח בכל חלון, BTC בפתיחה/סגירה, שעה UTC, יום בשבוע.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

_DB_PATH = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))) / "history.db"
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS window_results (
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
        """)
        _conn.commit()
    return _conn


def record_window_result(
    epoch: int,
    slug: str,
    side_won: Optional[str],
    btc_open: Optional[float] = None,
    btc_close: Optional[float] = None,
    window_sec: int = 300,
) -> bool:
    """שומר תוצאת חלון. מחזיר True אם נשמר בהצלחה."""
    try:
        import datetime
        conn = _get_conn()
        dt = datetime.datetime.utcfromtimestamp(epoch)
        conn.execute(
            """
            INSERT OR IGNORE INTO window_results
              (epoch, slug, window_sec, side_won, btc_open, btc_close, ts_recorded, hour_utc, weekday)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (epoch, slug, window_sec, side_won, btc_open, btc_close, time.time(), dt.hour, dt.weekday()),
        )
        conn.commit()
        return conn.total_changes > 0
    except Exception as e:
        print(f"[history_tracker] שגיאה בשמירה: {e}", flush=True)
        return False


def get_win_rate_stats(window_sec: int = 300) -> dict[str, Any]:
    """
    סטטיסטיקת ניצחון לפי: כולל, שעה UTC נוכחית, יום בשבוע נוכחי.
    """
    try:
        conn = _get_conn()

        # כולל
        cur = conn.execute(
            """
            SELECT
              COUNT(*) as total,
              SUM(CASE WHEN side_won='Up' THEN 1 ELSE 0 END) as up_wins,
              SUM(CASE WHEN side_won='Down' THEN 1 ELSE 0 END) as down_wins
            FROM window_results
            WHERE window_sec=? AND side_won IS NOT NULL
            """,
            (window_sec,),
        )
        row = cur.fetchone()
        total = row["total"] or 0
        up_wins = row["up_wins"] or 0
        down_wins = row["down_wins"] or 0

        # שעה UTC נוכחית
        now_hour = time.gmtime().tm_hour
        cur2 = conn.execute(
            """
            SELECT COUNT(*) as total,
              SUM(CASE WHEN side_won='Up' THEN 1 ELSE 0 END) as up_wins
            FROM window_results
            WHERE window_sec=? AND hour_utc=? AND side_won IS NOT NULL
            """,
            (window_sec, now_hour),
        )
        row2 = cur2.fetchone()
        hour_total = row2["total"] or 0
        hour_up = row2["up_wins"] or 0

        # יום שבוע נוכחי
        now_weekday = time.gmtime().tm_wday
        cur3 = conn.execute(
            """
            SELECT COUNT(*) as total,
              SUM(CASE WHEN side_won='Up' THEN 1 ELSE 0 END) as up_wins
            FROM window_results
            WHERE window_sec=? AND weekday=? AND side_won IS NOT NULL
            """,
            (window_sec, now_weekday),
        )
        row3 = cur3.fetchone()
        wd_total = row3["total"] or 0
        wd_up = row3["up_wins"] or 0

        # win streak אחרון (5 חלונות)
        cur4 = conn.execute(
            """
            SELECT side_won FROM window_results
            WHERE window_sec=? AND side_won IS NOT NULL
            ORDER BY epoch DESC LIMIT 5
            """,
            (window_sec,),
        )
        recent = [r["side_won"] for r in cur4.fetchall()]

        return {
            "available": total >= 5,
            "total_windows": total,
            "overall": {
                "up_wins": up_wins,
                "down_wins": down_wins,
                "up_rate": round(up_wins / total, 4) if total > 0 else None,
                "down_rate": round(down_wins / total, 4) if total > 0 else None,
            },
            "current_hour_utc": now_hour,
            "hour": {
                "total": hour_total,
                "up_wins": hour_up,
                "up_rate": round(hour_up / hour_total, 4) if hour_total > 0 else None,
                "down_rate": round((hour_total - hour_up) / hour_total, 4) if hour_total > 0 else None,
            },
            "current_weekday": now_weekday,
            "weekday": {
                "total": wd_total,
                "up_wins": wd_up,
                "up_rate": round(wd_up / wd_total, 4) if wd_total > 0 else None,
            },
            "recent_5": recent,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def get_recent_windows(limit: int = 20, window_sec: int = 300) -> list[dict]:
    """מחזיר חלונות אחרונים לתצוגה ב-UI."""
    try:
        conn = _get_conn()
        cur = conn.execute(
            """
            SELECT epoch, slug, side_won, btc_open, btc_close, ts_recorded
            FROM window_results
            WHERE window_sec=?
            ORDER BY epoch DESC LIMIT ?
            """,
            (window_sec, limit),
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []


def get_last_window_winners(
    window_sec: int = 300,
    limit: int = 5,
    min_drift_pct: float = 0.0,
) -> list[dict[str, Any]]:
    """N חלונות אחרונים שנסגרו עם תוצאה ידועה (פיצ'ר Follow Last Winner).

    - window_sec: סינון לפי 5m / 15m.
    - limit: כמה חלונות אחרונים להחזיר אחרי הסינון.
    - min_drift_pct: אם > 0, מסנן חלונות שהזזת BTC בהם < X% (רעש).
      0 = ללא סינון. אחוז מוחלט: |btc_close-btc_open|/btc_open*100.

    הסידור: epoch DESC — הראשון ברשימה הוא החלון העדכני ביותר.
    """
    try:
        conn = _get_conn()
        # שולפים יותר מהרצוי אם יש סינון drift — כדי שנוכל לחתוך אחרי הסינון.
        fetch_limit = max(limit * 6, limit) if min_drift_pct > 0 else limit
        cur = conn.execute(
            """
            SELECT epoch, slug, window_sec, side_won, btc_open, btc_close, ts_recorded
            FROM window_results
            WHERE window_sec = ? AND side_won IS NOT NULL
            ORDER BY epoch DESC LIMIT ?
            """,
            (window_sec, fetch_limit),
        )
        rows = [dict(row) for row in cur.fetchall()]
    except Exception:
        return []
    if min_drift_pct <= 0:
        return rows[:limit]
    filtered: list[dict[str, Any]] = []
    for r in rows:
        bo = r.get("btc_open")
        bc = r.get("btc_close")
        if bo is None or bc is None:
            continue
        try:
            bof = float(bo)
            bcf = float(bc)
        except (TypeError, ValueError):
            continue
        if bof <= 0:
            continue
        drift = abs(bcf - bof) / bof * 100.0
        if drift >= min_drift_pct:
            filtered.append(r)
        if len(filtered) >= limit:
            break
    return filtered


def get_hourly_breakdown(window_sec: int = 300) -> list[dict]:
    """פירוט win rate לכל שעה (0-23 UTC)."""
    try:
        conn = _get_conn()
        cur = conn.execute(
            """
            SELECT hour_utc,
              COUNT(*) as total,
              SUM(CASE WHEN side_won='Up' THEN 1 ELSE 0 END) as up_wins
            FROM window_results
            WHERE window_sec=? AND side_won IS NOT NULL
            GROUP BY hour_utc
            ORDER BY hour_utc
            """,
            (window_sec,),
        )
        rows = cur.fetchall()
        return [
            {
                "hour": row["hour_utc"],
                "total": row["total"],
                "up_wins": row["up_wins"],
                "up_rate": round(row["up_wins"] / row["total"], 4) if row["total"] > 0 else None,
            }
            for row in rows
        ]
    except Exception:
        return []
