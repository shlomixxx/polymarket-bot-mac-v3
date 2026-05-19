"""
Phase 2C: Strategy Analysis
DCA effectiveness, loss recovery ROI, TP analysis, side preference.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .db_migration import _get_conn, ensure_analytics_tables


def compute_dca_effectiveness(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Compare DCA trades (num_dca_slices > 1) vs single-entry trades.
    Shows whether DCA improves average entry and win rate.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    # Single entry
    single = conn.execute(f"""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl,
            AVG(avg_entry_price) as avg_entry,
            AVG(duration_sec) as avg_duration
        FROM sessions {where} AND num_dca_slices = 1
    """, params).fetchone()

    # DCA (multi-slice)
    dca = conn.execute(f"""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl,
            AVG(avg_entry_price) as avg_entry,
            AVG(duration_sec) as avg_duration,
            AVG(num_dca_slices) as avg_slices
        FROM sessions {where} AND num_dca_slices > 1
    """, params).fetchone()

    # DCA by slice count
    by_slices = conn.execute(f"""
        SELECT num_dca_slices,
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            AVG(realized_pnl) as avg_pnl,
            SUM(realized_pnl) as total_pnl
        FROM sessions {where} AND num_dca_slices IS NOT NULL
        GROUP BY num_dca_slices
        ORDER BY num_dca_slices
    """, params).fetchall()

    def _row_to_dict(r, label: str) -> dict:
        total = r["total"] or 0
        wins = r["wins"] or 0
        return {
            "label": label,
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
            "avg_entry_price": round(r["avg_entry"] or 0, 4) if r["avg_entry"] else None,
            "avg_duration_sec": round(r["avg_duration"] or 0, 1) if r["avg_duration"] else None,
        }

    single_data = _row_to_dict(single, "single_entry")
    dca_data = _row_to_dict(dca, "dca")
    if dca["avg_slices"]:
        dca_data["avg_slices"] = round(dca["avg_slices"], 1)

    slices_data = []
    for r in by_slices:
        total = r["total"] or 0
        wins = r["wins"] or 0
        slices_data.append({
            "slices": r["num_dca_slices"],
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
            "total_pnl": round(r["total_pnl"] or 0, 2),
        })

    return {
        "single_entry": single_data,
        "dca": dca_data,
        "by_slice_count": slices_data,
        "dca_improves_win_rate": (dca_data["win_rate_pct"] > single_data["win_rate_pct"])
            if single_data["total"] > 0 and dca_data["total"] > 0 else None,
    }


def compute_loss_recovery_analysis(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Analyze loss recovery effectiveness:
    - Sessions with multiplier > 1 vs multiplier = 1
    - ROI of recovery attempts
    - Cascade depth distribution
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    # Normal (no recovery)
    normal = conn.execute(f"""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl,
            AVG(total_invested_usd) as avg_invested
        FROM sessions {where}
        AND (loss_recovery_multiplier IS NULL OR loss_recovery_multiplier <= 1.0)
    """, params).fetchone()

    # Recovery (multiplier > 1)
    recovery = conn.execute(f"""
        SELECT COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl,
            AVG(total_invested_usd) as avg_invested,
            AVG(loss_recovery_multiplier) as avg_multiplier,
            MAX(loss_recovery_multiplier) as max_multiplier
        FROM sessions {where}
        AND loss_recovery_multiplier > 1.0
    """, params).fetchone()

    # By multiplier ranges
    multiplier_ranges = conn.execute(f"""
        SELECT
            CASE
                WHEN loss_recovery_multiplier <= 1 THEN '1x'
                WHEN loss_recovery_multiplier <= 3 THEN '2-3x'
                WHEN loss_recovery_multiplier <= 9 THEN '4-9x'
                WHEN loss_recovery_multiplier <= 27 THEN '10-27x'
                ELSE '28x+'
            END as range_label,
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl
        FROM sessions {where}
        AND loss_recovery_multiplier IS NOT NULL
        GROUP BY range_label
        ORDER BY MIN(loss_recovery_multiplier)
    """, params).fetchall()

    def _stats(r) -> dict:
        total = r["total"] or 0
        wins = r["wins"] or 0
        return {
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
            "avg_invested": round(r["avg_invested"] or 0, 2) if r["avg_invested"] else None,
        }

    normal_stats = _stats(normal)
    recovery_stats = _stats(recovery)
    if recovery["avg_multiplier"]:
        recovery_stats["avg_multiplier"] = round(recovery["avg_multiplier"], 2)
        recovery_stats["max_multiplier"] = round(recovery["max_multiplier"] or 0, 2)

    ranges = []
    for r in multiplier_ranges:
        total = r["total"] or 0
        wins = r["wins"] or 0
        ranges.append({
            "range": r["range_label"],
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
        })

    # Net ROI: did recovery trades make up for the extra risk?
    recovery_net = recovery_stats["total_pnl"]
    recovery_profitable = recovery_net > 0

    return {
        "normal": normal_stats,
        "recovery": recovery_stats,
        "by_multiplier_range": ranges,
        "recovery_net_profitable": recovery_profitable,
        "recovery_net_pnl": round(recovery_net, 2),
    }


def compute_tp_analysis(execution: Optional[str] = None) -> dict[str, Any]:
    """
    TP level analysis:
    - Actual vs peak unrealized (money left on table)
    - What-if analysis: if TP was higher/lower
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT exit_type, realized_pnl, peak_unrealized_pct,
               trough_unrealized_pct, total_invested_usd, duration_sec
        FROM sessions {where}
    """, params).fetchall()

    if not rows:
        return {"tp_sessions": 0, "analysis": None}

    tp_rows = [r for r in rows if r["exit_type"] == "TP"]
    expire_rows = [r for r in rows if r["exit_type"] in ("EXPIRE", "SETTLE_LOSS")]

    # For TP exits: how much more could they have made?
    tp_peak_analysis = []
    for r in tp_rows:
        peak = r["peak_unrealized_pct"]
        invested = r["total_invested_usd"] or 1
        actual_pnl = r["realized_pnl"] or 0
        actual_pct = 100 * actual_pnl / invested if invested > 0 else 0
        if peak is not None:
            tp_peak_analysis.append({
                "actual_pct": actual_pct,
                "peak_pct": peak,
                "left_on_table_pct": max(0, peak - actual_pct),
            })

    avg_left = (sum(x["left_on_table_pct"] for x in tp_peak_analysis) /
                len(tp_peak_analysis)) if tp_peak_analysis else None

    # For EXPIRE exits: did they ever hit a TP-worthy peak?
    expire_had_tp_chance = 0
    expire_peak_stats = []
    for r in expire_rows:
        peak = r["peak_unrealized_pct"]
        if peak is not None and peak > 0:
            expire_had_tp_chance += 1
            expire_peak_stats.append(peak)

    avg_expire_peak = (sum(expire_peak_stats) / len(expire_peak_stats)) if expire_peak_stats else None

    # TP timing: average duration of TP exits vs expiry
    tp_durations = [r["duration_sec"] for r in tp_rows if r["duration_sec"]]
    expire_durations = [r["duration_sec"] for r in expire_rows if r["duration_sec"]]

    return {
        "tp_sessions": len(tp_rows),
        "expire_sessions": len(expire_rows),
        "tp_avg_left_on_table_pct": round(avg_left, 2) if avg_left is not None else None,
        "expire_had_tp_chance_count": expire_had_tp_chance,
        "expire_had_tp_chance_pct": round(100 * expire_had_tp_chance / len(expire_rows), 2)
            if expire_rows else None,
        "expire_avg_peak_pct": round(avg_expire_peak, 2) if avg_expire_peak is not None else None,
        "tp_avg_duration_sec": round(sum(tp_durations) / len(tp_durations), 1)
            if tp_durations else None,
        "expire_avg_duration_sec": round(sum(expire_durations) / len(expire_durations), 1)
            if expire_durations else None,
    }


def compute_side_preference(execution: Optional[str] = None) -> dict[str, Any]:
    """Compare Up vs Down side performance."""
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL AND side IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT side,
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl,
            AVG(avg_entry_price) as avg_entry,
            AVG(duration_sec) as avg_duration
        FROM sessions {where}
        GROUP BY side
    """, params).fetchall()

    sides = {}
    for r in rows:
        total = r["total"] or 0
        wins = r["wins"] or 0
        sides[r["side"]] = {
            "total": total,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / total, 2) if total > 0 else 0,
            "total_pnl": round(r["total_pnl"] or 0, 2),
            "avg_pnl": round(r["avg_pnl"] or 0, 4),
            "avg_entry_price": round(r["avg_entry"] or 0, 4) if r["avg_entry"] else None,
            "avg_duration_sec": round(r["avg_duration"] or 0, 1) if r["avg_duration"] else None,
        }

    # Determine which side is better
    up = sides.get("Up", {})
    down = sides.get("Down", {})
    better_side = None
    if up.get("total", 0) >= 5 and down.get("total", 0) >= 5:
        if up.get("avg_pnl", 0) > down.get("avg_pnl", 0):
            better_side = "Up"
        elif down.get("avg_pnl", 0) > up.get("avg_pnl", 0):
            better_side = "Down"

    return {
        "by_side": sides,
        "better_side": better_side,
        "recommendation": f"Side '{better_side}' has higher avg PnL" if better_side else "Insufficient data or similar performance",
    }
