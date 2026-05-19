"""
Phase 4B: FastAPI Analytics Endpoints
All API routes for the V3 analytics dashboard.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query

from .db_migration import (
    ensure_analytics_tables,
    migrate_json_to_sqlite,
    get_analytics_db_stats,
)
from .core_metrics import compute_global_metrics, compute_equity_curve
from .timing_analysis import (
    compute_hourly_performance,
    compute_weekday_performance,
    compute_entry_minute_performance,
    compute_heatmap,
    compute_optimal_exit_timing,
)
from .strategy_analysis import (
    compute_dca_effectiveness,
    compute_loss_recovery_analysis,
    compute_tp_analysis,
    compute_side_preference,
)
from .risk_metrics import (
    compute_drawdown_curve,
    compute_slippage_analysis,
    compute_fee_impact,
    compute_risk_of_ruin,
    compute_pnl_distribution,
)
from .backtester import (
    what_if_tp_level,
    optimal_tp_search,
    what_if_entry_price,
    optimal_entry_search,
)
from .signal_quality import compute_signal_accuracy, compute_window_prediction_accuracy
from .market_regime import compute_volatility_regimes, compute_btc_direction_correlation
from .insights_engine import generate_insights, generate_config_recommendations

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ── DB Management ──────────────────────────────────────────────────────────

@router.post("/migrate")
async def api_migrate():
    """Run one-time JSON → SQLite migration."""
    result = migrate_json_to_sqlite()
    return {"status": "ok", "migrated": result}


@router.get("/db-stats")
async def api_db_stats():
    """Get counts for all analytics tables."""
    return get_analytics_db_stats()


# ── Core Metrics (Phase 2A) ───────────────────────────────────────────────

@router.get("/overview")
async def api_overview(execution: Optional[str] = Query(None)):
    """Global performance metrics."""
    return compute_global_metrics(execution)


@router.get("/equity-curve")
async def api_equity_curve(execution: Optional[str] = Query(None)):
    """Equity curve (cumulative PnL over time)."""
    return {"curve": compute_equity_curve(execution)}


# ── Timing Analysis (Phase 2B) ────────────────────────────────────────────

@router.get("/timing/hourly")
async def api_hourly(execution: Optional[str] = Query(None)):
    """Win rate and expectancy by UTC hour."""
    return {"hourly": compute_hourly_performance(execution)}


@router.get("/timing/weekday")
async def api_weekday(execution: Optional[str] = Query(None)):
    """Win rate by weekday."""
    return {"weekday": compute_weekday_performance(execution)}


@router.get("/timing/entry-minute")
async def api_entry_minute(execution: Optional[str] = Query(None)):
    """Win rate by entry minute within window."""
    return {"entry_minute": compute_entry_minute_performance(execution)}


@router.get("/timing/heatmap")
async def api_heatmap(execution: Optional[str] = Query(None)):
    """Hour x Weekday heatmap data."""
    return {"heatmap": compute_heatmap(execution)}


@router.get("/timing/optimal-exit")
async def api_optimal_exit(execution: Optional[str] = Query(None)):
    """Optimal exit timing analysis."""
    return compute_optimal_exit_timing(execution)


# ── Strategy Analysis (Phase 2C) ──────────────────────────────────────────

@router.get("/strategy/dca")
async def api_dca(execution: Optional[str] = Query(None)):
    """DCA effectiveness analysis."""
    return compute_dca_effectiveness(execution)


@router.get("/strategy/loss-recovery")
async def api_loss_recovery(execution: Optional[str] = Query(None)):
    """Loss recovery ROI analysis."""
    return compute_loss_recovery_analysis(execution)


@router.get("/strategy/tp-analysis")
async def api_tp_analysis(execution: Optional[str] = Query(None)):
    """Take profit level analysis."""
    return compute_tp_analysis(execution)


@router.get("/strategy/side-preference")
async def api_side_preference(execution: Optional[str] = Query(None)):
    """Up vs Down side comparison."""
    return compute_side_preference(execution)


# ── Risk Metrics (Phase 2D) ───────────────────────────────────────────────

@router.get("/risk/drawdown")
async def api_drawdown(execution: Optional[str] = Query(None)):
    """Drawdown curve and stats."""
    return compute_drawdown_curve(execution)


@router.get("/risk/slippage")
async def api_slippage(execution: Optional[str] = Query(None)):
    """Slippage analysis."""
    return compute_slippage_analysis(execution)


@router.get("/risk/fees")
async def api_fees(execution: Optional[str] = Query(None)):
    """Fee impact analysis."""
    return compute_fee_impact(execution)


@router.get("/risk/pnl-distribution")
async def api_pnl_distribution(execution: Optional[str] = Query(None)):
    """PnL distribution with histogram and percentiles."""
    return compute_pnl_distribution(execution)


@router.get("/risk/ruin")
async def api_risk_of_ruin(
    execution: Optional[str] = Query(None),
    bankroll: float = Query(10000),
    risk_pct: float = Query(2.5),
):
    """Risk of ruin estimation."""
    metrics = compute_global_metrics(execution)
    if metrics["total_sessions"] < 10:
        return {"error": "Need at least 10 sessions", "risk_of_ruin_pct": None}
    return compute_risk_of_ruin(
        win_rate=metrics["win_rate_pct"],
        avg_win=metrics["avg_win_usd"],
        avg_loss=metrics["avg_loss_usd"],
        bankroll=bankroll,
        risk_per_trade_pct=risk_pct,
    )


# ── Backtesting (Phase 3A) ────────────────────────────────────────────────

@router.get("/backtest/tp")
async def api_backtest_tp(
    tp_pct: float = Query(50),
    execution: Optional[str] = Query(None),
):
    """What-if: simulate different TP%."""
    return what_if_tp_level(tp_pct, execution)


@router.get("/backtest/optimal-tp")
async def api_optimal_tp(
    min_tp: float = Query(10),
    max_tp: float = Query(200),
    step: float = Query(10),
    execution: Optional[str] = Query(None),
):
    """Grid search for optimal TP%."""
    return optimal_tp_search((min_tp, max_tp), step, execution)


@router.get("/backtest/entry-price")
async def api_backtest_entry(
    entry_cents: float = Query(52),
    execution: Optional[str] = Query(None),
):
    """What-if: simulate different entry price thresholds."""
    return what_if_entry_price(entry_cents, execution)


@router.get("/backtest/optimal-entry")
async def api_optimal_entry(
    min_cents: float = Query(20),
    max_cents: float = Query(80),
    step: float = Query(5),
    execution: Optional[str] = Query(None),
):
    """Grid search for optimal entry price."""
    return optimal_entry_search((min_cents, max_cents), step, execution)


# ── Signal Quality (Phase 3B) ─────────────────────────────────────────────

@router.get("/signals/accuracy")
async def api_signal_accuracy(execution: Optional[str] = Query(None)):
    """Signal accuracy by gate and reason."""
    return compute_signal_accuracy(execution)


@router.get("/signals/window-prediction")
async def api_window_prediction():
    """How often did we pick the correct side?"""
    return compute_window_prediction_accuracy()


# ── Market Regime (Phase 3C) ──────────────────────────────────────────────

@router.get("/market/volatility-regimes")
async def api_volatility_regimes(execution: Optional[str] = Query(None)):
    """Win rate by BTC volatility regime."""
    return compute_volatility_regimes(execution)


@router.get("/market/btc-correlation")
async def api_btc_correlation(execution: Optional[str] = Query(None)):
    """BTC direction vs trading performance."""
    return compute_btc_direction_correlation(execution)


# ── Insights (Phase 5) ────────────────────────────────────────────────────

@router.get("/insights")
async def api_insights(execution: Optional[str] = Query(None)):
    """Automated insights and recommendations."""
    return generate_insights(execution)


@router.get("/recommendations")
async def api_recommendations(execution: Optional[str] = Query(None)):
    """Config parameter recommendations."""
    return generate_config_recommendations(execution)


# ── Full Report ────────────────────────────────────────────────────────────

@router.get("/full-report")
async def api_full_report(execution: Optional[str] = Query(None)):
    """Complete analytics report — all metrics in one call."""
    return {
        "overview": compute_global_metrics(execution),
        "timing": {
            "hourly": compute_hourly_performance(execution),
            "weekday": compute_weekday_performance(execution),
            "entry_minute": compute_entry_minute_performance(execution),
            "optimal_exit": compute_optimal_exit_timing(execution),
        },
        "strategy": {
            "dca": compute_dca_effectiveness(execution),
            "loss_recovery": compute_loss_recovery_analysis(execution),
            "tp_analysis": compute_tp_analysis(execution),
            "side_preference": compute_side_preference(execution),
        },
        "risk": {
            "drawdown": compute_drawdown_curve(execution),
            "slippage": compute_slippage_analysis(execution),
            "fees": compute_fee_impact(execution),
            "pnl_distribution": compute_pnl_distribution(execution),
        },
        "signals": {
            "accuracy": compute_signal_accuracy(execution),
            "window_prediction": compute_window_prediction_accuracy(),
        },
        "market": {
            "volatility_regimes": compute_volatility_regimes(execution),
            "btc_correlation": compute_btc_direction_correlation(execution),
        },
        "insights": generate_insights(execution),
        "recommendations": generate_config_recommendations(execution),
    }
