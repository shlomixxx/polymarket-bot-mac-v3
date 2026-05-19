"""
Phase 2A: Core Performance Metrics
Win rate, expectancy, profit factor, Sharpe, drawdown, R:R, streaks.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any, Optional

from .db_migration import _get_conn, ensure_analytics_tables


def _safe_div(a: float, b: float) -> Optional[float]:
    return a / b if b != 0 else None


def compute_global_metrics(execution: Optional[str] = None) -> dict[str, Any]:
    """
    מדדי ביצוע גלובליים על כל הסשנים הסגורים.
    execution: 'demo' / 'live' / None (all)
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT session_id, realized_pnl, exit_type, duration_sec,
               peak_unrealized_pct, trough_unrealized_pct,
               total_invested_usd, side, loss_recovery_multiplier,
               entry_ts, exit_ts
        FROM sessions {where}
        ORDER BY entry_ts ASC
    """, params).fetchall()

    if not rows:
        return _empty_metrics()

    total = len(rows)
    wins = [r for r in rows if (r["realized_pnl"] or 0) > 0]
    losses = [r for r in rows if (r["realized_pnl"] or 0) <= 0]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = 100.0 * win_count / total if total > 0 else 0

    # PnL sums
    total_pnl = sum(r["realized_pnl"] or 0 for r in rows)
    total_wins_sum = sum(r["realized_pnl"] or 0 for r in wins)
    total_losses_sum = abs(sum(r["realized_pnl"] or 0 for r in losses))

    avg_win = total_wins_sum / win_count if win_count > 0 else 0
    avg_loss = total_losses_sum / loss_count if loss_count > 0 else 0

    # Expectancy
    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)

    # Profit Factor
    profit_factor = _safe_div(total_wins_sum, total_losses_sum)

    # R:R
    rr_ratio = _safe_div(avg_win, avg_loss)

    # Sharpe Ratio (daily-ish: per session PnL)
    pnls = [r["realized_pnl"] or 0 for r in rows]
    sharpe = _compute_sharpe(pnls)

    # Max Drawdown
    max_dd, dd_curve = _compute_drawdown_curve(rows)

    # Recovery Factor
    recovery_factor = _safe_div(total_pnl, abs(max_dd)) if max_dd != 0 else None

    # Consecutive streaks
    streaks = _compute_streaks(rows)

    # By exit type
    tp_rows = [r for r in rows if r["exit_type"] == "TP"]
    expire_rows = [r for r in rows if r["exit_type"] == "EXPIRE"]
    settle_win_rows = [r for r in rows if r["exit_type"] == "SETTLE_WIN"]
    settle_loss_rows = [r for r in rows if r["exit_type"] == "SETTLE_LOSS"]

    # Peak unrealized left on table
    peak_left = []
    for r in wins:
        peak = r["peak_unrealized_pct"]
        pnl = r["realized_pnl"] or 0
        invested = r["total_invested_usd"] or 1
        actual_pct = 100 * pnl / invested if invested > 0 else 0
        if peak is not None and peak > actual_pct:
            peak_left.append(peak - actual_pct)

    avg_peak_left = sum(peak_left) / len(peak_left) if peak_left else None

    return {
        "total_sessions": total,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": round(win_rate, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "expectancy_usd": round(expectancy, 4),
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "rr_ratio": round(rr_ratio, 3) if rr_ratio is not None else None,
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown_usd": round(max_dd, 2),
        "recovery_factor": round(recovery_factor, 3) if recovery_factor is not None else None,
        "streaks": streaks,
        "by_exit_type": {
            "tp": {"count": len(tp_rows), "pnl": round(sum(r["realized_pnl"] or 0 for r in tp_rows), 2)},
            "expire": {"count": len(expire_rows), "pnl": round(sum(r["realized_pnl"] or 0 for r in expire_rows), 2)},
            "settle_win": {"count": len(settle_win_rows), "pnl": round(sum(r["realized_pnl"] or 0 for r in settle_win_rows), 2)},
            "settle_loss": {"count": len(settle_loss_rows), "pnl": round(sum(r["realized_pnl"] or 0 for r in settle_loss_rows), 2)},
        },
        "avg_peak_left_on_table_pct": round(avg_peak_left, 2) if avg_peak_left is not None else None,
        "avg_duration_sec": round(
            sum(r["duration_sec"] or 0 for r in rows if r["duration_sec"]) /
            max(sum(1 for r in rows if r["duration_sec"]), 1), 1
        ),
    }


def compute_equity_curve(execution: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Equity curve: cumulative PnL over time.
    Returns list of {ts, cumulative_pnl, session_id, pnl, exit_type, side}.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT session_id, exit_ts as ts, realized_pnl, exit_type, side
        FROM sessions {where}
        ORDER BY exit_ts ASC
    """, params).fetchall()

    curve = []
    cumulative = 0.0
    for r in rows:
        pnl = r["realized_pnl"] or 0
        cumulative += pnl
        curve.append({
            "ts": r["ts"],
            "cumulative_pnl": round(cumulative, 2),
            "pnl": round(pnl, 2),
            "session_id": r["session_id"],
            "exit_type": r["exit_type"],
            "side": r["side"],
        })
    return curve


def _compute_sharpe(pnls: list[float]) -> Optional[float]:
    if len(pnls) < 2:
        return None
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return mean / std


def _compute_drawdown_curve(rows: list[sqlite3.Row]) -> tuple[float, list[dict]]:
    """Returns (max_drawdown, drawdown_curve)."""
    peak_equity = 0.0
    cumulative = 0.0
    max_dd = 0.0
    curve = []

    for r in rows:
        pnl = r["realized_pnl"] or 0
        cumulative += pnl
        if cumulative > peak_equity:
            peak_equity = cumulative
        dd = cumulative - peak_equity  # negative or zero
        if dd < max_dd:
            max_dd = dd
        curve.append({
            "ts": r["exit_ts"] or r["entry_ts"],
            "equity": round(cumulative, 2),
            "drawdown": round(dd, 2),
            "drawdown_pct": round(100 * dd / peak_equity, 2) if peak_equity > 0 else 0,
        })
    return max_dd, curve


def _compute_streaks(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Compute consecutive win/loss streaks."""
    max_win_streak = 0
    max_loss_streak = 0
    current_streak = 0
    current_type = None  # 'win' or 'loss'

    streaks_list: list[dict] = []

    for r in rows:
        is_win = (r["realized_pnl"] or 0) > 0
        t = "win" if is_win else "loss"

        if t == current_type:
            current_streak += 1
        else:
            if current_type is not None:
                streaks_list.append({"type": current_type, "length": current_streak})
            current_type = t
            current_streak = 1

        if is_win:
            max_win_streak = max(max_win_streak, current_streak)
        else:
            max_loss_streak = max(max_loss_streak, current_streak)

    if current_type is not None:
        streaks_list.append({"type": current_type, "length": current_streak})

    # Current streak
    current = streaks_list[-1] if streaks_list else {"type": None, "length": 0}

    return {
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "current_streak_type": current["type"],
        "current_streak_length": current["length"],
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "total_sessions": 0,
        "win_count": 0,
        "loss_count": 0,
        "win_rate_pct": 0,
        "total_pnl_usd": 0,
        "avg_win_usd": 0,
        "avg_loss_usd": 0,
        "expectancy_usd": 0,
        "profit_factor": None,
        "rr_ratio": None,
        "sharpe_ratio": None,
        "max_drawdown_usd": 0,
        "recovery_factor": None,
        "streaks": {"max_win_streak": 0, "max_loss_streak": 0,
                    "current_streak_type": None, "current_streak_length": 0},
        "by_exit_type": {
            "tp": {"count": 0, "pnl": 0},
            "expire": {"count": 0, "pnl": 0},
            "settle_win": {"count": 0, "pnl": 0},
            "settle_loss": {"count": 0, "pnl": 0},
        },
        "avg_peak_left_on_table_pct": None,
        "avg_duration_sec": 0,
    }
