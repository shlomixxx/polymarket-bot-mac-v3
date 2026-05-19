"""
Phase 3A: Backtesting Engine
What-if simulator, optimal parameter search, pnl_path replay.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .db_migration import _get_conn, ensure_analytics_tables


def what_if_tp_level(tp_pct: float, execution: Optional[str] = None) -> dict[str, Any]:
    """
    What-if: simulate different TP% using pnl_path data.
    For each session, replay the pnl_path and check if tp_pct was reached.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    # Get sessions with pnl_snapshots
    where = "WHERE s.exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND s.execution=?"
        params.append(execution)

    sessions = conn.execute(f"""
        SELECT s.session_id, s.realized_pnl, s.exit_type, s.total_invested_usd,
               s.entry_ts, s.duration_sec, s.peak_unrealized_pct
        FROM sessions s {where}
    """, params).fetchall()

    if not sessions:
        return {"tp_pct": tp_pct, "total_sessions": 0}

    simulated_wins = 0
    simulated_total_pnl = 0.0
    sessions_with_data = 0
    tp_hits = 0

    for s in sessions:
        peak = s["peak_unrealized_pct"]
        invested = s["total_invested_usd"] or 1

        if peak is None:
            # No peak data, use actual result
            pnl = s["realized_pnl"] or 0
            simulated_total_pnl += pnl
            if pnl > 0:
                simulated_wins += 1
            sessions_with_data += 1
            continue

        sessions_with_data += 1

        if peak >= tp_pct:
            # Would have hit this TP
            tp_hits += 1
            simulated_pnl = invested * tp_pct / 100.0
            simulated_total_pnl += simulated_pnl
            simulated_wins += 1
        else:
            # TP not reached — use actual result (loss or lower TP)
            pnl = s["realized_pnl"] or 0
            simulated_total_pnl += pnl
            if pnl > 0:
                simulated_wins += 1

    win_rate = 100.0 * simulated_wins / sessions_with_data if sessions_with_data > 0 else 0
    avg_pnl = simulated_total_pnl / sessions_with_data if sessions_with_data > 0 else 0

    return {
        "tp_pct": tp_pct,
        "total_sessions": sessions_with_data,
        "simulated_wins": simulated_wins,
        "simulated_win_rate_pct": round(win_rate, 2),
        "simulated_total_pnl": round(simulated_total_pnl, 2),
        "simulated_avg_pnl": round(avg_pnl, 4),
        "tp_would_have_hit": tp_hits,
        "tp_hit_rate_pct": round(100 * tp_hits / sessions_with_data, 2) if sessions_with_data > 0 else 0,
    }


def optimal_tp_search(
    tp_range: tuple[float, float] = (10, 200),
    step: float = 10,
    execution: Optional[str] = None,
) -> dict[str, Any]:
    """
    Grid search over TP% values to find optimal take-profit level.
    """
    results = []
    current = tp_range[0]
    while current <= tp_range[1]:
        result = what_if_tp_level(current, execution)
        results.append(result)
        current += step

    if not results:
        return {"optimal_tp_pct": None, "results": []}

    # Best by avg PnL
    best_avg = max(results, key=lambda r: r["simulated_avg_pnl"])
    # Best by total PnL
    best_total = max(results, key=lambda r: r["simulated_total_pnl"])

    return {
        "optimal_tp_by_avg_pnl": best_avg["tp_pct"],
        "optimal_tp_by_total_pnl": best_total["tp_pct"],
        "results": results,
    }


def what_if_entry_price(entry_cents: float, execution: Optional[str] = None) -> dict[str, Any]:
    """
    What-if: simulate different entry price thresholds.
    Filters sessions by avg_entry_price vs threshold.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL AND avg_entry_price IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    threshold = entry_cents / 100.0

    # Sessions where entry would have been accepted (entry <= threshold)
    accepted = conn.execute(f"""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl
        FROM sessions {where} AND avg_entry_price <= ?
    """, params + [threshold]).fetchone()

    # Sessions filtered out
    rejected = conn.execute(f"""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl
        FROM sessions {where} AND avg_entry_price > ?
    """, params + [threshold]).fetchone()

    def _stats(r) -> dict:
        total = r["total"] or 0
        wins = r["wins"] or 0
        return {
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
        }

    return {
        "entry_cents": entry_cents,
        "accepted": _stats(accepted),
        "rejected": _stats(rejected),
        "filter_improves_avg_pnl": (
            (_stats(accepted)["avg_pnl"] > _stats(rejected)["avg_pnl"])
            if accepted["total"] and rejected["total"] else None
        ),
    }


def optimal_entry_search(
    range_cents: tuple[float, float] = (20, 80),
    step: float = 5,
    execution: Optional[str] = None,
) -> dict[str, Any]:
    """Grid search over entry price thresholds."""
    results = []
    current = range_cents[0]
    while current <= range_cents[1]:
        result = what_if_entry_price(current, execution)
        results.append(result)
        current += step

    if not results:
        return {"optimal_entry_cents": None, "results": []}

    valid = [r for r in results if r["accepted"]["total"] >= 5]
    best = max(valid, key=lambda r: r["accepted"]["avg_pnl"]) if valid else None

    return {
        "optimal_entry_cents": best["entry_cents"] if best else None,
        "results": results,
    }
