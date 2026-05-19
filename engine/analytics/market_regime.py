"""
Phase 3C: Market Regime Detection
Volatility regimes, trending vs range, BTC movement correlation.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .db_migration import _get_conn, ensure_analytics_tables


def compute_volatility_regimes(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Classify windows by BTC volatility and analyze win rate per regime.
    Uses btc_open/btc_close from window_results to measure per-window volatility.
    """
    conn = _get_conn()

    # Get windows with BTC data and match to sessions
    rows = conn.execute("""
        SELECT w.epoch, w.btc_open, w.btc_close, w.side_won,
               s.session_id, s.realized_pnl, s.side, s.exit_type
        FROM window_results w
        LEFT JOIN sessions s ON w.epoch = s.epoch AND s.exit_type IS NOT NULL
        WHERE w.btc_open IS NOT NULL AND w.btc_close IS NOT NULL
              AND w.btc_open > 0
        ORDER BY w.epoch ASC
    """).fetchall()

    if not rows:
        return {"regimes": [], "note": "No data"}

    # Compute volatility (abs % change) per window
    vol_data = []
    for r in rows:
        btc_change_pct = abs((r["btc_close"] - r["btc_open"]) / r["btc_open"] * 100)
        vol_data.append({
            "epoch": r["epoch"],
            "btc_change_pct": btc_change_pct,
            "side_won": r["side_won"],
            "had_trade": r["session_id"] is not None,
            "pnl": r["realized_pnl"],
            "traded_side": r["side"],
        })

    # Classify into regimes by percentile
    changes = sorted([v["btc_change_pct"] for v in vol_data])
    n = len(changes)
    p33 = changes[int(n * 0.33)] if n > 3 else 0.05
    p66 = changes[int(n * 0.66)] if n > 3 else 0.10

    regimes = {
        "low_vol": {"label": f"Low (<{p33:.3f}%)", "windows": 0, "trades": 0, "wins": 0, "total_pnl": 0},
        "mid_vol": {"label": f"Mid ({p33:.3f}-{p66:.3f}%)", "windows": 0, "trades": 0, "wins": 0, "total_pnl": 0},
        "high_vol": {"label": f"High (>{p66:.3f}%)", "windows": 0, "trades": 0, "wins": 0, "total_pnl": 0},
    }

    for v in vol_data:
        if v["btc_change_pct"] < p33:
            regime = "low_vol"
        elif v["btc_change_pct"] < p66:
            regime = "mid_vol"
        else:
            regime = "high_vol"

        regimes[regime]["windows"] += 1
        if v["had_trade"]:
            regimes[regime]["trades"] += 1
            if (v["pnl"] or 0) > 0:
                regimes[regime]["wins"] += 1
            regimes[regime]["total_pnl"] += v["pnl"] or 0

    result_regimes = []
    for key, data in regimes.items():
        result_regimes.append({
            "regime": key,
            "label": data["label"],
            "windows": data["windows"],
            "trades": data["trades"],
            "wins": data["wins"],
            "win_rate_pct": round(100 * data["wins"] / data["trades"], 2) if data["trades"] > 0 else 0,
            "total_pnl": round(data["total_pnl"], 2),
            "avg_pnl": round(data["total_pnl"] / data["trades"], 4) if data["trades"] > 0 else 0,
        })

    # Which regime is best?
    traded_regimes = [r for r in result_regimes if r["trades"] >= 5]
    best = max(traded_regimes, key=lambda r: r["avg_pnl"]) if traded_regimes else None

    return {
        "regimes": result_regimes,
        "thresholds": {"p33": round(p33, 4), "p66": round(p66, 4)},
        "best_regime": best["regime"] if best else None,
        "total_windows": len(vol_data),
    }


def compute_btc_direction_correlation(execution: Optional[str] = None) -> dict[str, Any]:
    """
    Correlation between BTC price direction and trading performance.
    """
    conn = _get_conn()

    rows = conn.execute("""
        SELECT w.btc_open, w.btc_close, w.side_won,
               s.realized_pnl, s.side as traded_side, s.exit_type
        FROM window_results w
        JOIN sessions s ON w.epoch = s.epoch
        WHERE s.exit_type IS NOT NULL
              AND w.btc_open IS NOT NULL AND w.btc_close IS NOT NULL
    """).fetchall()

    if not rows:
        return {"total": 0}

    # BTC went up vs down
    btc_up = [r for r in rows if r["btc_close"] > r["btc_open"]]
    btc_down = [r for r in rows if r["btc_close"] <= r["btc_open"]]

    def _analyze(group, label):
        if not group:
            return {"label": label, "total": 0, "wins": 0, "win_rate_pct": 0, "avg_pnl": 0}
        wins = sum(1 for r in group if (r["realized_pnl"] or 0) > 0)
        total_pnl = sum(r["realized_pnl"] or 0 for r in group)
        return {
            "label": label,
            "total": len(group),
            "wins": wins,
            "win_rate_pct": round(100 * wins / len(group), 2),
            "avg_pnl": round(total_pnl / len(group), 4),
            "total_pnl": round(total_pnl, 2),
        }

    # Traded correct direction?
    aligned = [r for r in rows if
               (r["traded_side"] == "Up" and r["btc_close"] > r["btc_open"]) or
               (r["traded_side"] == "Down" and r["btc_close"] <= r["btc_open"])]
    misaligned = [r for r in rows if r not in aligned]

    return {
        "total": len(rows),
        "btc_went_up": _analyze(btc_up, "BTC Up"),
        "btc_went_down": _analyze(btc_down, "BTC Down"),
        "aligned_with_btc": _analyze(aligned, "Aligned"),
        "misaligned_with_btc": _analyze(misaligned, "Misaligned"),
    }
