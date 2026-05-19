"""
Phase 2B: Timing Analysis
Win rate by hour, weekday, entry minute in window, heatmap data.
"""
from __future__ import annotations

from typing import Any, Optional

from .db_migration import _get_conn, ensure_analytics_tables


def compute_hourly_performance(execution: Optional[str] = None) -> list[dict[str, Any]]:
    """Win rate and expectancy by UTC hour (0-23)."""
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL AND hour_utc IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT hour_utc,
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl,
            SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) as sum_wins,
            SUM(CASE WHEN realized_pnl <= 0 THEN realized_pnl ELSE 0 END) as sum_losses
        FROM sessions {where}
        GROUP BY hour_utc
        ORDER BY hour_utc
    """, params).fetchall()

    result = []
    for r in rows:
        total = r["total"]
        wins = r["wins"]
        win_rate = 100.0 * wins / total if total > 0 else 0
        avg_win = r["sum_wins"] / wins if wins > 0 else 0
        avg_loss = abs(r["sum_losses"] / r["losses"]) if r["losses"] > 0 else 0
        expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)
        result.append({
            "hour": r["hour_utc"],
            "total": total,
            "wins": wins,
            "losses": r["losses"],
            "win_rate_pct": round(win_rate, 2),
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
            "expectancy": round(expectancy, 4),
        })
    return result


def compute_weekday_performance(execution: Optional[str] = None) -> list[dict[str, Any]]:
    """Win rate and expectancy by weekday (0=Mon, 6=Sun)."""
    ensure_analytics_tables()
    conn = _get_conn()

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    where = "WHERE exit_type IS NOT NULL AND weekday IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT weekday,
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl
        FROM sessions {where}
        GROUP BY weekday
        ORDER BY weekday
    """, params).fetchall()

    result = []
    for r in rows:
        total = r["total"]
        wins = r["wins"]
        result.append({
            "weekday": r["weekday"],
            "day_name": day_names[r["weekday"]] if 0 <= r["weekday"] <= 6 else "?",
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
        })
    return result


def compute_entry_minute_performance(execution: Optional[str] = None) -> list[dict[str, Any]]:
    """Win rate by entry minute within the 5-min window."""
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE s.exit_type IS NOT NULL AND s.epoch IS NOT NULL AND s.entry_ts IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND s.execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT
            CAST((s.entry_ts - s.epoch) / 60 AS INTEGER) as entry_minute,
            COUNT(*) as total,
            SUM(CASE WHEN s.realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(s.realized_pnl) as total_pnl,
            AVG(s.realized_pnl) as avg_pnl
        FROM sessions s {where}
        GROUP BY entry_minute
        ORDER BY entry_minute
    """, params).fetchall()

    result = []
    for r in rows:
        total = r["total"]
        wins = r["wins"]
        result.append({
            "entry_minute": r["entry_minute"],
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
        })
    return result


def compute_heatmap(execution: Optional[str] = None) -> list[dict[str, Any]]:
    """Hour × Weekday heatmap data for win rate."""
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL AND hour_utc IS NOT NULL AND weekday IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT hour_utc, weekday,
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl
        FROM sessions {where}
        GROUP BY hour_utc, weekday
        ORDER BY weekday, hour_utc
    """, params).fetchall()

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    result = []
    for r in rows:
        total = r["total"]
        result.append({
            "hour": r["hour_utc"],
            "weekday": r["weekday"],
            "day_name": day_names[r["weekday"]] if 0 <= r["weekday"] <= 6 else "?",
            "total": total,
            "wins": r["wins"],
            "win_rate_pct": round(100.0 * r["wins"] / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
        })
    return result


def compute_optimal_exit_timing(execution: Optional[str] = None) -> dict[str, Any]:
    """Analyze holding duration vs outcome to find optimal exit timing."""
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL AND duration_sec IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT duration_sec, realized_pnl, exit_type
        FROM sessions {where}
        ORDER BY duration_sec ASC
    """, params).fetchall()

    # Time buckets
    buckets = [
        ("0-15s", 0, 15),
        ("15-30s", 15, 30),
        ("30-60s", 30, 60),
        ("60-120s", 60, 120),
        ("120-180s", 120, 180),
        ("180-300s", 180, 300),
        ("300s+", 300, float("inf")),
    ]

    bucket_data = []
    for label, lo, hi in buckets:
        group = [r for r in rows if lo <= (r["duration_sec"] or 0) < hi]
        if not group:
            bucket_data.append({
                "bucket": label, "count": 0, "wins": 0,
                "win_rate_pct": 0, "avg_pnl": 0, "total_pnl": 0,
            })
            continue

        wins = sum(1 for r in group if (r["realized_pnl"] or 0) > 0)
        total_pnl = sum(r["realized_pnl"] or 0 for r in group)
        bucket_data.append({
            "bucket": label,
            "count": len(group),
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / len(group), 2),
            "avg_pnl": round(total_pnl / len(group), 4),
            "total_pnl": round(total_pnl, 2),
        })

    # Find optimal bucket (highest avg_pnl with min 5 trades)
    eligible = [b for b in bucket_data if b["count"] >= 5]
    optimal = max(eligible, key=lambda b: b["avg_pnl"]) if eligible else None

    return {
        "buckets": bucket_data,
        "optimal_bucket": optimal["bucket"] if optimal else None,
        "optimal_avg_pnl": optimal["avg_pnl"] if optimal else None,
    }
