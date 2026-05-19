"""
Phase 5: Automated Insights & Recommendations
Generates actionable insights from analytics data.
"""
from __future__ import annotations

from typing import Any, Optional

from .core_metrics import compute_global_metrics
from .timing_analysis import (
    compute_hourly_performance,
    compute_weekday_performance,
    compute_entry_minute_performance,
    compute_optimal_exit_timing,
)
from .strategy_analysis import (
    compute_dca_effectiveness,
    compute_loss_recovery_analysis,
    compute_tp_analysis,
    compute_side_preference,
)
from .risk_metrics import compute_risk_of_ruin, compute_fee_impact


def generate_insights(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Generate automated insights and recommendations based on all analytics.
    Returns categorized insights: critical, warnings, opportunities, info.
    """
    insights: list[dict[str, Any]] = []

    # 1. Global metrics insights
    try:
        metrics = compute_global_metrics(execution)
        _analyze_global_metrics(metrics, insights)
    except Exception:
        pass

    # 2. Timing insights
    try:
        hourly = compute_hourly_performance(execution)
        _analyze_timing(hourly, insights)
    except Exception:
        pass

    # 3. Strategy insights
    try:
        dca = compute_dca_effectiveness(execution)
        _analyze_dca(dca, insights)
    except Exception:
        pass

    try:
        recovery = compute_loss_recovery_analysis(execution)
        _analyze_recovery(recovery, insights)
    except Exception:
        pass

    try:
        tp = compute_tp_analysis(execution)
        _analyze_tp(tp, insights)
    except Exception:
        pass

    try:
        side = compute_side_preference(execution)
        _analyze_side(side, insights)
    except Exception:
        pass

    # 4. Fee impact
    try:
        fees = compute_fee_impact(execution)
        _analyze_fees(fees, insights)
    except Exception:
        pass

    # 5. Risk of ruin
    try:
        if metrics["total_sessions"] >= 20:
            ror = compute_risk_of_ruin(
                win_rate=metrics["win_rate_pct"],
                avg_win=metrics["avg_win_usd"],
                avg_loss=metrics["avg_loss_usd"],
            )
            _analyze_risk(ror, insights)
    except Exception:
        pass

    # Sort: critical first, then warnings, opportunities, info
    priority = {"critical": 0, "warning": 1, "opportunity": 2, "info": 3}
    insights.sort(key=lambda x: priority.get(x.get("level", "info"), 4))

    return {
        "total_insights": len(insights),
        "insights": insights,
        "critical_count": sum(1 for i in insights if i["level"] == "critical"),
        "warning_count": sum(1 for i in insights if i["level"] == "warning"),
        "opportunity_count": sum(1 for i in insights if i["level"] == "opportunity"),
    }


def generate_config_recommendations(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Generate specific config parameter recommendations based on data.
    """
    recommendations: list[dict[str, Any]] = []

    try:
        metrics = compute_global_metrics(execution)
        tp = compute_tp_analysis(execution)
        side = compute_side_preference(execution)
        timing = compute_optimal_exit_timing(execution)
        hourly = compute_hourly_performance(execution)
        dca = compute_dca_effectiveness(execution)

        # TP recommendation
        if tp.get("tp_avg_left_on_table_pct") and tp["tp_avg_left_on_table_pct"] > 20:
            recommendations.append({
                "param": "take_profit_pct",
                "current_issue": f"Average {tp['tp_avg_left_on_table_pct']:.0f}% left on table after TP",
                "suggestion": "Consider increasing TP% to capture more upside",
                "confidence": "medium",
            })

        if tp.get("expire_had_tp_chance_pct") and tp["expire_had_tp_chance_pct"] > 60:
            recommendations.append({
                "param": "take_profit_pct",
                "current_issue": f"{tp['expire_had_tp_chance_pct']:.0f}% of expired trades had positive peak",
                "suggestion": "Consider lower TP% — many trades peak and then expire as losses",
                "confidence": "high",
            })

        # Side preference
        if side.get("better_side"):
            better = side["better_side"]
            by_side = side.get("by_side", {})
            better_data = by_side.get(better, {})
            recommendations.append({
                "param": "side_preference",
                "current_issue": f"'{better}' has higher avg PnL ({better_data.get('avg_pnl', 0):.4f})",
                "suggestion": f"Consider preferring '{better}' side",
                "confidence": "medium" if better_data.get("total", 0) >= 30 else "low",
            })

        # DCA recommendation
        if dca.get("dca_improves_win_rate") is True:
            recommendations.append({
                "param": "dca_enabled",
                "current_issue": "DCA improves win rate over single entry",
                "suggestion": "Keep DCA enabled",
                "confidence": "medium",
            })
        elif dca.get("dca_improves_win_rate") is False:
            recommendations.append({
                "param": "dca_enabled",
                "current_issue": "DCA does NOT improve win rate over single entry",
                "suggestion": "Consider disabling DCA or adjusting slice parameters",
                "confidence": "medium",
            })

        # Timing: worst hours
        if hourly:
            bad_hours = [h for h in hourly if h["total"] >= 10 and h["avg_pnl"] < 0]
            if bad_hours:
                worst = min(bad_hours, key=lambda h: h["avg_pnl"])
                recommendations.append({
                    "param": "trading_schedule",
                    "current_issue": f"Hour {worst['hour']}:00 UTC has avg PnL of ${worst['avg_pnl']:.4f}",
                    "suggestion": f"Consider pausing the bot during hour {worst['hour']}:00 UTC",
                    "confidence": "high" if worst["total"] >= 30 else "medium",
                })

    except Exception:
        pass

    return {
        "recommendations": recommendations,
        "total": len(recommendations),
    }


# ── Private insight analyzers ──────────────────────────────────────────────

def _analyze_global_metrics(m: dict, insights: list) -> None:
    if m["total_sessions"] < 10:
        insights.append({
            "level": "info",
            "category": "data",
            "message": f"Only {m['total_sessions']} sessions — insights will improve with more data",
        })
        return

    if m["win_rate_pct"] < 40:
        insights.append({
            "level": "critical",
            "category": "performance",
            "message": f"Win rate is low at {m['win_rate_pct']:.1f}% — review entry criteria",
        })
    elif m["win_rate_pct"] > 60:
        insights.append({
            "level": "info",
            "category": "performance",
            "message": f"Strong win rate at {m['win_rate_pct']:.1f}%",
        })

    if m["expectancy_usd"] < 0:
        insights.append({
            "level": "critical",
            "category": "performance",
            "message": f"Negative expectancy (${m['expectancy_usd']:.4f}/trade) — system is losing money per trade",
        })

    if m["max_drawdown_usd"] < -50:
        insights.append({
            "level": "warning",
            "category": "risk",
            "message": f"Max drawdown reached ${abs(m['max_drawdown_usd']):.2f}",
        })

    streaks = m.get("streaks", {})
    if streaks.get("max_loss_streak", 0) >= 5:
        insights.append({
            "level": "warning",
            "category": "risk",
            "message": f"Experienced {streaks['max_loss_streak']}-trade losing streak",
        })


def _analyze_timing(hourly: list, insights: list) -> None:
    if not hourly:
        return

    profitable_hours = [h for h in hourly if h["total"] >= 10 and h["avg_pnl"] > 0]
    losing_hours = [h for h in hourly if h["total"] >= 10 and h["avg_pnl"] < 0]

    if losing_hours:
        worst = min(losing_hours, key=lambda h: h["avg_pnl"])
        insights.append({
            "level": "opportunity",
            "category": "timing",
            "message": f"Hour {worst['hour']}:00 UTC is consistently unprofitable "
                       f"({worst['win_rate_pct']:.0f}% WR, ${worst['avg_pnl']:.4f}/trade over {worst['total']} trades)",
        })

    if profitable_hours:
        best = max(profitable_hours, key=lambda h: h["avg_pnl"])
        insights.append({
            "level": "info",
            "category": "timing",
            "message": f"Best hour: {best['hour']}:00 UTC "
                       f"({best['win_rate_pct']:.0f}% WR, ${best['avg_pnl']:.4f}/trade over {best['total']} trades)",
        })


def _analyze_dca(dca: dict, insights: list) -> None:
    single = dca.get("single_entry", {})
    dca_data = dca.get("dca", {})
    if single.get("total", 0) < 5 or dca_data.get("total", 0) < 5:
        return

    diff = dca_data.get("win_rate_pct", 0) - single.get("win_rate_pct", 0)
    if diff > 5:
        insights.append({
            "level": "info",
            "category": "strategy",
            "message": f"DCA improves win rate by {diff:.1f}% over single entry",
        })
    elif diff < -5:
        insights.append({
            "level": "warning",
            "category": "strategy",
            "message": f"DCA reduces win rate by {abs(diff):.1f}% vs single entry — consider disabling",
        })


def _analyze_recovery(recovery: dict, insights: list) -> None:
    rec = recovery.get("recovery", {})
    if rec.get("total", 0) < 5:
        return

    if not recovery.get("recovery_net_profitable"):
        insights.append({
            "level": "warning",
            "category": "strategy",
            "message": f"Loss recovery is net LOSING (${recovery['recovery_net_pnl']:.2f}) — "
                       "multiplier trades are not recovering losses",
        })
    else:
        insights.append({
            "level": "info",
            "category": "strategy",
            "message": f"Loss recovery is net PROFITABLE (${recovery['recovery_net_pnl']:.2f})",
        })


def _analyze_tp(tp: dict, insights: list) -> None:
    if tp.get("tp_avg_left_on_table_pct") and tp["tp_avg_left_on_table_pct"] > 30:
        insights.append({
            "level": "opportunity",
            "category": "strategy",
            "message": f"On avg {tp['tp_avg_left_on_table_pct']:.0f}% unrealized gain left on table after TP — "
                       "consider increasing TP target",
        })

    if tp.get("expire_had_tp_chance_pct") and tp["expire_had_tp_chance_pct"] > 50:
        insights.append({
            "level": "opportunity",
            "category": "strategy",
            "message": f"{tp['expire_had_tp_chance_pct']:.0f}% of losing trades had a positive peak — "
                       "a lower TP could capture these",
        })


def _analyze_side(side: dict, insights: list) -> None:
    by_side = side.get("by_side", {})
    up = by_side.get("Up", {})
    down = by_side.get("Down", {})
    if up.get("total", 0) < 10 or down.get("total", 0) < 10:
        return

    diff = abs(up.get("win_rate_pct", 0) - down.get("win_rate_pct", 0))
    if diff > 10:
        better = "Up" if up.get("win_rate_pct", 0) > down.get("win_rate_pct", 0) else "Down"
        insights.append({
            "level": "opportunity",
            "category": "strategy",
            "message": f"'{better}' side outperforms by {diff:.1f}% win rate — consider side preference",
        })


def _analyze_fees(fees: dict, insights: list) -> None:
    if fees.get("fee_drag_pct") and fees["fee_drag_pct"] > 20:
        insights.append({
            "level": "warning",
            "category": "costs",
            "message": f"Fees consume {fees['fee_drag_pct']:.1f}% of gross profit (${fees['total_fees_usd']:.2f} total)",
        })


def _analyze_risk(ror: dict, insights: list) -> None:
    risk = ror.get("risk_of_ruin_pct")
    if risk is not None and risk > 10:
        insights.append({
            "level": "critical",
            "category": "risk",
            "message": f"Risk of ruin estimated at {risk:.1f}% — consider reducing position size",
        })

    if ror.get("kelly_fraction") and ror["kelly_fraction"] > 0:
        insights.append({
            "level": "info",
            "category": "risk",
            "message": f"Half-Kelly suggests risking {ror['suggested_risk_pct']:.1f}% per trade",
        })
