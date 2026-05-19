"""
Phase 3B: Signal Quality Analysis
Measures accuracy of trading signals vs actual outcomes.
"""
from __future__ import annotations

from typing import Any, Optional

from .db_migration import _get_conn, ensure_analytics_tables


def compute_signal_accuracy(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Analyze how well the entry 'reason' / 'gate' predicted outcomes.
    Uses the reason field from BUY trades correlated with session outcome.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE s.exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND s.execution=?"
        params.append(execution)

    # Get sessions with their entry reason from the first BUY trade
    rows = conn.execute(f"""
        SELECT s.session_id, s.realized_pnl, s.side, s.exit_type,
               t.reason, t.gate
        FROM sessions s
        LEFT JOIN trades t ON t.session_id = s.session_id AND t.type = 'BUY'
        {where}
        GROUP BY s.session_id
    """, params).fetchall()

    if not rows:
        return {"total": 0, "by_gate": {}, "by_reason_prefix": {}}

    # Analyze by gate value
    by_gate: dict[str, dict] = {}
    for r in rows:
        gate = r["gate"] or "unknown"
        if gate not in by_gate:
            by_gate[gate] = {"total": 0, "wins": 0, "total_pnl": 0}
        by_gate[gate]["total"] += 1
        if (r["realized_pnl"] or 0) > 0:
            by_gate[gate]["wins"] += 1
        by_gate[gate]["total_pnl"] += r["realized_pnl"] or 0

    gate_stats = {}
    for gate, data in by_gate.items():
        total = data["total"]
        gate_stats[gate] = {
            "total": total,
            "wins": data["wins"],
            "win_rate_pct": round(100 * data["wins"] / total, 2) if total > 0 else 0,
            "total_pnl": round(data["total_pnl"], 2),
            "avg_pnl": round(data["total_pnl"] / total, 4) if total > 0 else 0,
        }

    # Analyze by reason prefix (e.g., "entry_ok:Down", "entry_ok:Up")
    by_reason: dict[str, dict] = {}
    for r in rows:
        reason = r["reason"] or "unknown"
        # Extract prefix like "entry_ok:Up" or "entry_ok:Down"
        prefix = reason.split(" ")[0] if reason else "unknown"
        if prefix not in by_reason:
            by_reason[prefix] = {"total": 0, "wins": 0, "total_pnl": 0}
        by_reason[prefix]["total"] += 1
        if (r["realized_pnl"] or 0) > 0:
            by_reason[prefix]["wins"] += 1
        by_reason[prefix]["total_pnl"] += r["realized_pnl"] or 0

    reason_stats = {}
    for prefix, data in by_reason.items():
        total = data["total"]
        reason_stats[prefix] = {
            "total": total,
            "wins": data["wins"],
            "win_rate_pct": round(100 * data["wins"] / total, 2) if total > 0 else 0,
            "total_pnl": round(data["total_pnl"], 2),
            "avg_pnl": round(data["total_pnl"] / total, 4) if total > 0 else 0,
        }

    return {
        "total": len(rows),
        "by_gate": gate_stats,
        "by_reason_prefix": reason_stats,
    }


def compute_window_prediction_accuracy() -> dict[str, Any]:
    """
    Compare window_results (Up/Down) with the side the bot actually traded.
    Did we pick the right side?
    """
    ensure_analytics_tables()
    conn = _get_conn()

    rows = conn.execute("""
        SELECT s.session_id, s.side as traded_side, s.realized_pnl, s.epoch,
               w.side_won
        FROM sessions s
        JOIN window_results w ON s.epoch = w.epoch
        WHERE s.exit_type IS NOT NULL AND w.side_won IS NOT NULL
    """).fetchall()

    if not rows:
        return {"total": 0, "correct_side_count": 0}

    correct = 0
    wrong = 0
    correct_pnl = 0
    wrong_pnl = 0

    for r in rows:
        if r["traded_side"] == r["side_won"]:
            correct += 1
            correct_pnl += r["realized_pnl"] or 0
        else:
            wrong += 1
            wrong_pnl += r["realized_pnl"] or 0

    total = correct + wrong
    return {
        "total": total,
        "correct_side_count": correct,
        "wrong_side_count": wrong,
        "side_accuracy_pct": round(100 * correct / total, 2) if total > 0 else 0,
        "correct_side_avg_pnl": round(correct_pnl / correct, 4) if correct > 0 else 0,
        "wrong_side_avg_pnl": round(wrong_pnl / wrong, 4) if wrong > 0 else 0,
    }
