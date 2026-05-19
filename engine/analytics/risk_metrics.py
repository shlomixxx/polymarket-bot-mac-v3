"""
Phase 2D: Risk Metrics
Drawdown curve, slippage analysis, fee impact, risk of ruin.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .db_migration import _get_conn, ensure_analytics_tables


def compute_drawdown_curve(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Full drawdown curve with equity peak tracking.
    Returns equity curve and drawdown overlay.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT session_id, exit_ts, realized_pnl, exit_type, side
        FROM sessions {where}
        ORDER BY exit_ts ASC
    """, params).fetchall()

    if not rows:
        return {"curve": [], "max_drawdown_usd": 0, "max_drawdown_pct": 0,
                "current_drawdown_usd": 0, "recovery_trades": 0}

    peak_equity = 0.0
    cumulative = 0.0
    max_dd = 0.0
    max_dd_pct = 0.0
    curve = []
    dd_start_idx = None
    longest_dd_trades = 0
    current_dd_trades = 0

    for r in rows:
        pnl = r["realized_pnl"] or 0
        cumulative += pnl

        if cumulative > peak_equity:
            peak_equity = cumulative
            if current_dd_trades > longest_dd_trades:
                longest_dd_trades = current_dd_trades
            current_dd_trades = 0
        else:
            current_dd_trades += 1

        dd = cumulative - peak_equity
        dd_pct = (dd / peak_equity * 100) if peak_equity > 0 else 0

        if dd < max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

        curve.append({
            "ts": r["exit_ts"],
            "equity": round(cumulative, 2),
            "peak_equity": round(peak_equity, 2),
            "drawdown": round(dd, 2),
            "drawdown_pct": round(dd_pct, 2),
        })

    current_dd = cumulative - peak_equity

    return {
        "curve": curve,
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "current_drawdown_usd": round(current_dd, 2),
        "longest_dd_trades": max(longest_dd_trades, current_dd_trades),
        "total_equity": round(cumulative, 2),
        "peak_equity": round(peak_equity, 2),
    }


def compute_slippage_analysis(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Analyze slippage: difference between limit_price and actual price.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE type='BUY' AND limit_price IS NOT NULL AND price IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT price, limit_price, contracts, effective_investment_usd
        FROM trades {where}
    """, params).fetchall()

    if not rows:
        return {"total_trades": 0, "avg_slippage_cents": 0, "total_slippage_usd": 0}

    slippages = []
    total_slippage_usd = 0
    for r in rows:
        actual = r["price"] or 0
        limit = r["limit_price"] or 0
        contracts = r["contracts"] or 0
        slip = actual - limit  # positive = worse than expected
        slip_usd = slip * contracts
        slippages.append({
            "slip_cents": round(slip * 100, 4),
            "slip_usd": round(slip_usd, 4),
        })
        total_slippage_usd += slip_usd

    avg_slip_cents = sum(s["slip_cents"] for s in slippages) / len(slippages)

    # Distribution
    positive_slip = [s for s in slippages if s["slip_cents"] > 0]  # worse
    negative_slip = [s for s in slippages if s["slip_cents"] < 0]  # better
    zero_slip = [s for s in slippages if s["slip_cents"] == 0]

    return {
        "total_trades": len(rows),
        "avg_slippage_cents": round(avg_slip_cents, 4),
        "total_slippage_usd": round(total_slippage_usd, 2),
        "worse_than_limit_count": len(positive_slip),
        "better_than_limit_count": len(negative_slip),
        "exact_fill_count": len(zero_slip),
        "worse_pct": round(100 * len(positive_slip) / len(slippages), 1) if slippages else 0,
    }


def compute_fee_impact(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Total fees paid and their impact on profitability.
    """
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE fee_est IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    row = conn.execute(f"""
        SELECT
            COUNT(*) as total_trades,
            SUM(fee_est) as total_fees,
            AVG(fee_est) as avg_fee,
            SUM(CASE WHEN type='BUY' THEN fee_est ELSE 0 END) as entry_fees,
            SUM(CASE WHEN type!='BUY' THEN fee_est ELSE 0 END) as exit_fees
        FROM trades {where}
    """, params).fetchone()

    # Compare to total PnL
    pnl_row = conn.execute("""
        SELECT SUM(realized_pnl) as total_pnl
        FROM sessions WHERE exit_type IS NOT NULL
    """).fetchone()

    total_pnl = pnl_row["total_pnl"] or 0
    total_fees = row["total_fees"] or 0
    gross_pnl = total_pnl + total_fees  # What PnL would be without fees

    return {
        "total_trades": row["total_trades"] or 0,
        "total_fees_usd": round(total_fees, 2),
        "avg_fee_usd": round(row["avg_fee"] or 0, 4),
        "entry_fees_usd": round(row["entry_fees"] or 0, 2),
        "exit_fees_usd": round(row["exit_fees"] or 0, 2),
        "net_pnl_usd": round(total_pnl, 2),
        "gross_pnl_without_fees": round(gross_pnl, 2),
        "fee_drag_pct": round(100 * total_fees / gross_pnl, 2) if gross_pnl != 0 else 0,
    }


def compute_risk_of_ruin(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    bankroll: float = 10000,
    risk_per_trade_pct: float = 2.5,
    ruin_threshold_pct: float = 50,
) -> dict[str, Any]:
    """
    Monte Carlo risk of ruin estimation.
    Uses the analytical formula for geometric risk of ruin.
    """
    if avg_loss == 0 or win_rate <= 0 or win_rate >= 100:
        return {"risk_of_ruin_pct": None, "note": "Insufficient data"}

    wr = win_rate / 100.0
    risk_amount = bankroll * risk_per_trade_pct / 100.0
    ruin_level = bankroll * (1 - ruin_threshold_pct / 100.0)

    # Simplified analytical approximation
    # Using the classic formula: RoR = ((1-edge)/edge)^(bankroll_units)
    edge = wr * avg_win - (1 - wr) * avg_loss
    if edge <= 0:
        return {
            "risk_of_ruin_pct": 100.0,
            "edge_per_trade": round(edge, 4),
            "note": "Negative edge — risk of ruin is 100%",
        }

    # Kelly criterion
    if avg_loss > 0:
        kelly_fraction = (wr * avg_win - (1 - wr) * avg_loss) / avg_win if avg_win > 0 else 0
    else:
        kelly_fraction = 0

    # Approximate RoR using exponential formula
    variance = wr * avg_win**2 + (1 - wr) * avg_loss**2
    if variance > 0 and edge > 0:
        units_to_ruin = ruin_level / risk_amount if risk_amount > 0 else float("inf")
        ror = math.exp(-2 * edge * units_to_ruin / variance)
        ror = min(ror, 1.0)
    else:
        ror = 1.0

    return {
        "risk_of_ruin_pct": round(ror * 100, 4),
        "edge_per_trade": round(edge, 4),
        "kelly_fraction": round(kelly_fraction, 4),
        "suggested_risk_pct": round(kelly_fraction * 100 / 2, 2),  # Half-Kelly
        "note": f"Based on {win_rate:.1f}% win rate, avg win ${avg_win:.2f}, avg loss ${avg_loss:.2f}",
    }


def compute_pnl_distribution(execution: Optional[str] = None) -> dict[str, Any]:
    """PnL distribution: histogram + percentiles."""
    ensure_analytics_tables()
    conn = _get_conn()

    where = "WHERE exit_type IS NOT NULL AND realized_pnl IS NOT NULL"
    params: list[Any] = []
    if execution:
        where += " AND execution=?"
        params.append(execution)

    rows = conn.execute(f"""
        SELECT realized_pnl FROM sessions {where} ORDER BY realized_pnl ASC
    """, params).fetchall()

    pnls = [r["realized_pnl"] for r in rows]
    if not pnls:
        return {"count": 0, "percentiles": {}, "histogram": []}

    n = len(pnls)

    def percentile(p: float) -> float:
        idx = (n - 1) * p
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return pnls[lo]
        return pnls[lo] + (pnls[hi] - pnls[lo]) * (idx - lo)

    # Histogram bins
    min_pnl = pnls[0]
    max_pnl = pnls[-1]
    num_bins = min(20, max(5, n // 5))
    bin_width = (max_pnl - min_pnl) / num_bins if num_bins > 0 and max_pnl != min_pnl else 1
    histogram = []
    for i in range(num_bins):
        lo = min_pnl + i * bin_width
        hi = lo + bin_width
        count = sum(1 for p in pnls if lo <= p < hi) if i < num_bins - 1 else sum(1 for p in pnls if lo <= p <= hi)
        histogram.append({
            "bin_start": round(lo, 2),
            "bin_end": round(hi, 2),
            "count": count,
        })

    return {
        "count": n,
        "mean": round(sum(pnls) / n, 4),
        "median": round(percentile(0.5), 4),
        "std": round(math.sqrt(sum((p - sum(pnls) / n) ** 2 for p in pnls) / max(n - 1, 1)), 4),
        "min": round(min_pnl, 4),
        "max": round(max_pnl, 4),
        "percentiles": {
            "p5": round(percentile(0.05), 4),
            "p10": round(percentile(0.10), 4),
            "p25": round(percentile(0.25), 4),
            "p50": round(percentile(0.50), 4),
            "p75": round(percentile(0.75), 4),
            "p90": round(percentile(0.90), 4),
            "p95": round(percentile(0.95), 4),
        },
        "histogram": histogram,
    }
