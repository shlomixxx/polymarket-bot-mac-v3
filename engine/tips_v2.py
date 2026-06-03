from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict
from collections import defaultdict

FEE_RATE = 0.002  # must match demo_engine.py

_DATA_ROOT = os.environ.get("DATA_ROOT")
if os.environ.get("LOG_RUNS_ROOT"):
    RUNS_ROOT = Path(os.environ["LOG_RUNS_ROOT"]).resolve()
elif _DATA_ROOT:
    RUNS_ROOT = Path(_DATA_ROOT).resolve() / "logs" / "runs"
else:
    RUNS_ROOT = Path(__file__).resolve().parent.parent / "logs" / "runs"


def _safe_float(x: Any) -> float | None:
    try:
        v = float(x)
    except Exception:
        return None
    return v if v == v else None


def _safe_int(x: Any) -> int | None:
    try:
        v = int(x)
    except Exception:
        return None
    return v


def _group_trades_by_session(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_sid: DefaultDict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        # פוזיציות מ-reconcile (chain) — לא חלק מסטטיסטיקת הריצה/עצות (Tips v2)
        if t.get("reconcile_origin"):
            continue
        sid = t.get("session_id")
        tid = t.get("id")
        if not sid:
            # fallback: like run_logging.py grouping
            if t.get("type") == "BUY" and tid:
                sid = str(tid)
            else:
                sid = f"orphan-{tid or 'unknown'}-{t.get('token_id') or ''}"
        by_sid[str(sid)].append(t)
    for lst in by_sid.values():
        lst.sort(key=lambda x: float(x.get("ts") or 0))
    return dict(by_sid)


def _exit_trade_from_session(session_trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    exit_types = (
        "SELL_TP",
        "EXPIRE_0",
        "SETTLE_WIN",
        "SETTLE_LOSS",
        "SETTLE_UNKNOWN",
    )
    exits = [t for t in session_trades if t.get("type") in exit_types]
    if not exits:
        return None
    exits.sort(key=lambda x: float(x.get("ts") or 0))
    return exits[-1]


def _compute_expectancy(tp_count: int, tp_sum: float, expire_count: int, expire_sum_neg: float) -> dict[str, float]:
    """
    expire_sum_neg is sum of realized_pnl (negative values). We use abs().
    """
    total = tp_count + expire_count
    if total <= 0:
        return {
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss_abs": 0.0,
            "rr": 0.0,
            "expectancy": 0.0,
        }

    win_rate = tp_count / total if total else 0.0
    avg_win = tp_sum / tp_count if tp_count else 0.0
    avg_loss_abs = (abs(expire_sum_neg) / expire_count) if expire_count else 0.0
    rr = (avg_win / avg_loss_abs) if avg_loss_abs > 0 else 0.0
    expectancy = win_rate * avg_win - (1.0 - win_rate) * avg_loss_abs
    return {
        "win_rate": win_rate * 100.0,
        "avg_win": avg_win,
        "avg_loss_abs": avg_loss_abs,
        "rr": rr,
        "expectancy": expectancy,
    }


def _compute_leg_cost_from_tp_exit(tp_exit: dict[str, Any]) -> float | None:
    """
    Matches frontend legCostFromTpExit:
    proceeds = price * contracts * (1 - FEE_RATE)
    leg_cost = proceeds - realized_pnl
    """
    contracts = _safe_float(tp_exit.get("contracts"))
    px = _safe_float(tp_exit.get("price"))
    realized = _safe_float(tp_exit.get("realized_pnl"))
    if contracts is None or px is None or realized is None:
        return None
    proceeds = px * contracts * (1.0 - FEE_RATE)
    leg_cost = proceeds - realized
    return leg_cost if leg_cost > 0 else None


@dataclass(frozen=True)
class SessionOutcome:
    """מחזור בודד (session) לאגרגציית מדדים מורחבים."""

    exit_type: str  # "TP" | "EXPIRE"
    pnl: float
    duration_sec: float | None
    side: str
    entry_spread: float | None  # ask−bid בדולרים (מ־BUY ראשון)


def _entry_spread_from_first_buy(first_buy: dict[str, Any] | None, side: str) -> float | None:
    if not first_buy:
        return None
    if side == "Up":
        au = _safe_float(first_buy.get("ask_u"))
        bu = _safe_float(first_buy.get("bid_u"))
        if au is not None and bu is not None and au >= bu:
            return au - bu
    if side == "Down":
        ad = _safe_float(first_buy.get("ask_d"))
        bd = _safe_float(first_buy.get("bid_d"))
        if ad is not None and bd is not None and ad >= bd:
            return ad - bd
    return None


def _percentile_sorted(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    idx = (n - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _flatten_session_outcomes(runs: list[RunStats]) -> list[SessionOutcome]:
    out: list[SessionOutcome] = []
    for r in runs:
        out.extend(r.session_outcomes)
    return out


TIME_BUCKETS: list[tuple[str, float, float]] = [
    ("0–30 שנ׳", 0.0, 30.0),
    ("30–60 שנ׳", 30.0, 60.0),
    ("60–120 שנ׳", 60.0, 120.0),
    ("120+ שנ׳", 120.0, float("inf")),
]


def _build_time_buckets(outcomes: list[SessionOutcome]) -> list[dict[str, Any]]:
    """קבוצות זמן החזקה עם תוחלת לכל קבוצה."""
    rows: list[dict[str, Any]] = []
    for label, lo, hi in TIME_BUCKETS:
        group = [o for o in outcomes if o.duration_sec is not None and lo <= o.duration_sec < hi]
        tp = [o for o in group if o.exit_type == "TP"]
        ex = [o for o in group if o.exit_type == "EXPIRE"]
        tp_sum = sum(o.pnl for o in tp)
        ex_sum = sum(o.pnl for o in ex)
        calc = _compute_expectancy(len(tp), tp_sum, len(ex), ex_sum)
        rows.append(
            {
                "bucket": label,
                "count": len(group),
                "tp_count": len(tp),
                "expire_count": len(ex),
                "expectancy": calc["expectancy"] if group else None,
                "win_rate": calc["win_rate"] if group else None,
                "avg_win": calc["avg_win"] if group else None,
                "avg_loss_abs": calc["avg_loss_abs"] if group else None,
            }
        )
    return rows


def _optimal_exit_bucket(time_buckets: list[dict[str, Any]], min_count: int = 5) -> dict[str, Any] | None:
    """הקבוצה עם תוחלת הגבוהה ביותר שיש בה מספיק מחזורים."""
    eligible = [b for b in time_buckets if b["count"] >= min_count and b["expectancy"] is not None]
    if not eligible:
        return None
    return max(eligible, key=lambda b: b["expectancy"])  # type: ignore[return-value]


def _build_extended_metrics(outcomes: list[SessionOutcome], min_samples: int) -> dict[str, Any]:
    """מדדים מורחבים לפי כל מחזורי ה-session בחלון."""
    _empty_dur = {"duration_min": None, "duration_max": None,
                  "duration_p10": None, "duration_p50": None, "duration_p90": None,
                  "duration_p10_tp": None, "duration_p50_tp": None, "duration_p90_tp": None,
                  "duration_p10_expire": None, "duration_p50_expire": None, "duration_p90_expire": None,
                  "time_buckets": _build_time_buckets([]),
                  "optimal_exit_bucket": None}
    if not outcomes:
        return {
            "profit_factor": None,
            "avg_duration_tp_sec": None,
            "avg_duration_expire_sec": None,
            "pnl_percentiles": None,
            "entry_spread_avg_usd": None,
            "entry_spread_avg_cents": None,
            "entry_spread_sessions": 0,
            "by_side": {"Up": _empty_side_metrics(), "Down": _empty_side_metrics()},
            "low_sample_warning": True,
            "low_sample_message": f"אין מחזורים — נדרש מידע (סף מינימום להמלצות: {min_samples}).",
            **_empty_dur,
        }

    n = len(outcomes)
    tp_pnls = [o.pnl for o in outcomes if o.exit_type == "TP"]
    ex_pnls = [o.pnl for o in outcomes if o.exit_type == "EXPIRE"]
    sum_tp = float(sum(tp_pnls))
    sum_ex = float(sum(ex_pnls))
    gross_loss = abs(sum_ex) if ex_pnls else 0.0
    profit_factor: float | None
    if gross_loss > 1e-12:
        profit_factor = sum_tp / gross_loss
    elif sum_tp > 0:
        profit_factor = None
    else:
        profit_factor = None

    dur_tp = [o.duration_sec for o in outcomes if o.exit_type == "TP" and o.duration_sec is not None]
    dur_ex = [o.duration_sec for o in outcomes if o.exit_type == "EXPIRE" and o.duration_sec is not None]
    avg_dur_tp = sum(dur_tp) / len(dur_tp) if dur_tp else None
    avg_dur_ex = sum(dur_ex) / len(dur_ex) if dur_ex else None

    pnls = [o.pnl for o in outcomes]
    sn = sorted(pnls)
    pnl_percentiles = {
        "p10": _percentile_sorted(sn, 0.10),
        "p50": _percentile_sorted(sn, 0.50),
        "p90": _percentile_sorted(sn, 0.90),
        "min": float(min(sn)),
        "max": float(max(sn)),
    }

    spreads = [o.entry_spread for o in outcomes if o.entry_spread is not None]
    avg_spread = sum(spreads) / len(spreads) if spreads else None

    def _side_metrics(side: str) -> dict[str, Any]:
        sub = [o for o in outcomes if o.side == side]
        if not sub:
            return _empty_side_metrics()
        tp_c = sum(1 for x in sub if x.exit_type == "TP")
        ex_c = sum(1 for x in sub if x.exit_type == "EXPIRE")
        tp_s = sum(x.pnl for x in sub if x.exit_type == "TP")
        ex_sn = sum(x.pnl for x in sub if x.exit_type == "EXPIRE")
        ex = _compute_expectancy(tp_c, tp_s, ex_c, ex_sn)
        return {
            "sessions": len(sub),
            "expectancy": float(ex["expectancy"]),
            "tp_win_pct": float(ex["win_rate"]),
            "tp_count": tp_c,
            "expire_count": ex_c,
        }

    # ─── Duration distribution ────────────────────────────────────────────────
    all_durs = sorted([o.duration_sec for o in outcomes if o.duration_sec is not None])
    tp_durs = sorted([o.duration_sec for o in outcomes if o.exit_type == "TP" and o.duration_sec is not None])
    ex_durs = sorted([o.duration_sec for o in outcomes if o.exit_type == "EXPIRE" and o.duration_sec is not None])

    def _dur_pcts(lst: list[float]) -> tuple[float | None, float | None, float | None]:
        return (
            _percentile_sorted(lst, 0.10),
            _percentile_sorted(lst, 0.50),
            _percentile_sorted(lst, 0.90),
        )

    dur_p10, dur_p50, dur_p90 = _dur_pcts(all_durs)
    dur_p10_tp, dur_p50_tp, dur_p90_tp = _dur_pcts(tp_durs)
    dur_p10_ex, dur_p50_ex, dur_p90_ex = _dur_pcts(ex_durs)

    time_buckets = _build_time_buckets(outcomes)
    optimal_bucket = _optimal_exit_bucket(time_buckets)

    low = n < min_samples
    return {
        "profit_factor": profit_factor,
        "avg_duration_tp_sec": avg_dur_tp,
        "avg_duration_expire_sec": avg_dur_ex,
        "pnl_percentiles": pnl_percentiles,
        "entry_spread_avg_usd": avg_spread,
        "entry_spread_avg_cents": (avg_spread * 100.0) if avg_spread is not None else None,
        "entry_spread_sessions": len(spreads),
        "by_side": {"Up": _side_metrics("Up"), "Down": _side_metrics("Down")},
        "low_sample_warning": low,
        "low_sample_message": (
            f"רק {n} מחזורים בחלון זה — פחות מסף {min_samples}; המלצות זהירות."
            if low
            else None
        ),
        # ─── Duration distribution ────────────────────────────────────────
        "duration_min": float(min(all_durs)) if all_durs else None,
        "duration_max": float(max(all_durs)) if all_durs else None,
        "duration_p10": dur_p10,
        "duration_p50": dur_p50,
        "duration_p90": dur_p90,
        "duration_p10_tp": dur_p10_tp,
        "duration_p50_tp": dur_p50_tp,
        "duration_p90_tp": dur_p90_tp,
        "duration_p10_expire": dur_p10_ex,
        "duration_p50_expire": dur_p50_ex,
        "duration_p90_expire": dur_p90_ex,
        "time_buckets": time_buckets,
        "optimal_exit_bucket": optimal_bucket,
    }


def _empty_side_metrics() -> dict[str, Any]:
    return {"sessions": 0, "expectancy": None, "tp_win_pct": None, "tp_count": 0, "expire_count": 0}


def _window_comparison_slice(bundle: dict[str, Any]) -> dict[str, Any]:
    em = bundle.get("extended_metrics") or {}
    gm = bundle.get("global_metrics") or {}
    dq = bundle.get("data_quality") or {}
    return {
        "expectancy": gm.get("expectancy"),
        "sessions_total": dq.get("sessions_total"),
        "profit_factor": em.get("profit_factor"),
        "tp_win_pct": gm.get("win_rate"),
    }


def _bid_from_hypothetical_pct(leg_cost: float, contracts: float, pct: float) -> float | None:
    """
    Mirrors frontend bidFromHypotheticalPct:
    leg_val = leg_cost * (1 + pct/100)
    bid = leg_val / (contracts * (1 - FEE_RATE))
    """
    if contracts <= 0 or leg_cost <= 0:
        return None
    if not (pct == pct):  # NaN check
        return None
    leg_val = leg_cost * (1.0 + pct / 100.0)
    denom = contracts * (1.0 - FEE_RATE)
    if denom <= 0:
        return None
    return leg_val / denom


@dataclass(frozen=True)
class RunStats:
    strategy_config: dict[str, Any]
    tp_count: int
    tp_sum: float
    expire_count: int
    expire_sum_neg: float
    expectancy: dict[str, float]
    expire_rate: float
    avg_expire_loss_abs: float
    # for evidence
    tp_peak_sum: float
    tp_peak_count: int
    tp_trough_sum: float
    tp_trough_count: int
    expire_trough_sum: float
    expire_trough_count: int
    # optional after-TP evidence (vs exit)
    after_tp_peak_delta_cents_sum: float
    after_tp_peak_delta_cents_count: int
    after_tp_peak_delta_pct_sum: float
    after_tp_peak_delta_pct_count: int
    after_tp_trough_delta_cents_sum: float
    after_tp_trough_delta_cents_count: int
    after_tp_trough_delta_pct_sum: float
    after_tp_trough_delta_pct_count: int
    # context
    entries_count_sum: float
    entries_count_sessions: int
    duration_sec_sum: float
    duration_sec_sessions: int
    # שוק Polymarket (5m / 15m) מתוך strategy_snapshot — להפרדת טיפים
    btc_window: str = "5m"
    session_outcomes: tuple[SessionOutcome, ...] = ()


def _normalize_btc_window(cfg: dict[str, Any]) -> str:
    bw = str(cfg.get("btc_window") or "5m")
    return bw if bw in ("5m", "15m") else "5m"


def _analyze_trades(trades: list[dict[str, Any]], strategy_config: dict[str, Any]) -> RunStats:
    by_sid = _group_trades_by_session(trades)

    tp_count = 0
    tp_sum = 0.0
    expire_count = 0
    expire_sum_neg = 0.0

    tp_peak_sum = 0.0
    tp_peak_count = 0
    tp_trough_sum = 0.0
    tp_trough_count = 0

    expire_trough_sum = 0.0
    expire_trough_count = 0

    after_tp_peak_delta_cents_sum = 0.0
    after_tp_peak_delta_cents_count = 0
    after_tp_peak_delta_pct_sum = 0.0
    after_tp_peak_delta_pct_count = 0

    after_tp_trough_delta_cents_sum = 0.0
    after_tp_trough_delta_cents_count = 0
    after_tp_trough_delta_pct_sum = 0.0
    after_tp_trough_delta_pct_count = 0

    entries_count_sum = 0.0
    entries_count_sessions = 0
    duration_sec_sum = 0.0
    duration_sec_sessions = 0
    session_outcomes_list: list[SessionOutcome] = []

    for _, session_trades in by_sid.items():
        exit_trade = _exit_trade_from_session(session_trades)
        if not exit_trade:
            continue

        # context: number of BUY legs and approximate duration
        buys = [t for t in session_trades if t.get("type") == "BUY"]
        entries_count_sum += float(len(buys))
        entries_count_sessions += 1
        start_ts = None
        if buys:
            ts_buys = [_safe_float(t.get("ts")) for t in buys]
            ts_buys = [x for x in ts_buys if x is not None]
            start_ts = min(ts_buys) if ts_buys else None
        if start_ts is None:
            # fallback: min ts among session
            ts_vals = [_safe_float(t.get("ts")) for t in session_trades]
            ts_vals = [x for x in ts_vals if x is not None]
            start_ts = min(ts_vals) if ts_vals else None
        exit_ts = _safe_float(exit_trade.get("ts"))
        session_duration: float | None = None
        if start_ts is not None and exit_ts is not None and exit_ts >= start_ts:
            session_duration = exit_ts - start_ts
            duration_sec_sum += session_duration
            duration_sec_sessions += 1

        realized = _safe_float(exit_trade.get("realized_pnl")) or 0.0
        typ = exit_trade.get("type")

        buys_sorted = sorted(buys, key=lambda x: float(x.get("ts") or 0))
        first_buy = buys_sorted[0] if buys_sorted else None
        side = str(exit_trade.get("side") or (first_buy.get("side") if first_buy else "") or "")
        spread = _entry_spread_from_first_buy(first_buy, side)
        if typ == "SELL_TP" or typ == "SETTLE_WIN":
            exit_kind = "TP"
        else:
            exit_kind = "EXPIRE"
        session_outcomes_list.append(SessionOutcome(exit_kind, realized, session_duration, side, spread))

        if typ == "SELL_TP":
            tp_count += 1
            tp_sum += realized
            peak = _safe_float(exit_trade.get("peak_unrealized_pct"))
            trough = _safe_float(exit_trade.get("trough_unrealized_pct"))
            if peak is not None:
                tp_peak_sum += peak
                tp_peak_count += 1
            if trough is not None:
                tp_trough_sum += trough
                tp_trough_count += 1

            # after TP evidence (only SELL_TP trades)
            leg_cost = _compute_leg_cost_from_tp_exit(exit_trade)
            contracts = _safe_float(exit_trade.get("contracts"))
            exit_bid = _safe_float(exit_trade.get("price"))
            potential_peak = _safe_float(exit_trade.get("potential_peak_unrealized_pct"))
            potential_trough = _safe_float(exit_trade.get("potential_trough_unrealized_pct"))
            if leg_cost is not None and contracts is not None and exit_bid is not None:
                if potential_peak is not None:
                    bid_peak_h = _bid_from_hypothetical_pct(leg_cost, contracts, potential_peak)
                    if bid_peak_h is not None:
                        delta_cents = (bid_peak_h - exit_bid) * 100.0
                        after_tp_peak_delta_cents_sum += delta_cents
                        after_tp_peak_delta_cents_count += 1
                        # percent vs exit (exitBid is in dollars)
                        if exit_bid > 0:
                            after_tp_peak_delta_pct_sum += ((bid_peak_h - exit_bid) / exit_bid) * 100.0
                            after_tp_peak_delta_pct_count += 1
                if potential_trough is not None:
                    bid_trough_h = _bid_from_hypothetical_pct(leg_cost, contracts, potential_trough)
                    if bid_trough_h is not None:
                        delta_cents = (bid_trough_h - exit_bid) * 100.0
                        after_tp_trough_delta_cents_sum += delta_cents
                        after_tp_trough_delta_cents_count += 1
                        if exit_bid > 0:
                            after_tp_trough_delta_pct_sum += ((bid_trough_h - exit_bid) / exit_bid) * 100.0
                            after_tp_trough_delta_pct_count += 1

        elif typ in ("EXPIRE_0", "SETTLE_LOSS", "SETTLE_UNKNOWN"):
            expire_count += 1
            expire_sum_neg += realized
            trough = _safe_float(exit_trade.get("trough_unrealized_pct"))
            if trough is not None:
                expire_trough_sum += trough
                expire_trough_count += 1
        elif typ == "SETTLE_WIN":
            tp_count += 1
            tp_sum += realized
            peak = _safe_float(exit_trade.get("peak_unrealized_pct"))
            trough = _safe_float(exit_trade.get("trough_unrealized_pct"))
            if peak is not None:
                tp_peak_sum += peak
                tp_peak_count += 1
            if trough is not None:
                tp_trough_sum += trough
                tp_trough_count += 1

    expectancy = _compute_expectancy(tp_count, tp_sum, expire_count, expire_sum_neg)
    total = tp_count + expire_count
    expire_rate = expire_count / total if total else 0.0
    avg_expire_loss_abs = abs(expire_sum_neg) / expire_count if expire_count else 0.0

    return RunStats(
        strategy_config=strategy_config,
        tp_count=tp_count,
        tp_sum=tp_sum,
        expire_count=expire_count,
        expire_sum_neg=expire_sum_neg,
        expectancy=expectancy,
        expire_rate=expire_rate,
        avg_expire_loss_abs=avg_expire_loss_abs,
        tp_peak_sum=tp_peak_sum,
        tp_peak_count=tp_peak_count,
        tp_trough_sum=tp_trough_sum,
        tp_trough_count=tp_trough_count,
        expire_trough_sum=expire_trough_sum,
        expire_trough_count=expire_trough_count,
        after_tp_peak_delta_cents_sum=after_tp_peak_delta_cents_sum,
        after_tp_peak_delta_cents_count=after_tp_peak_delta_cents_count,
        after_tp_peak_delta_pct_sum=after_tp_peak_delta_pct_sum,
        after_tp_peak_delta_pct_count=after_tp_peak_delta_pct_count,
        after_tp_trough_delta_cents_sum=after_tp_trough_delta_cents_sum,
        after_tp_trough_delta_cents_count=after_tp_trough_delta_cents_count,
        after_tp_trough_delta_pct_sum=after_tp_trough_delta_pct_sum,
        after_tp_trough_delta_pct_count=after_tp_trough_delta_pct_count,
        entries_count_sum=entries_count_sum,
        entries_count_sessions=entries_count_sessions,
        duration_sec_sum=duration_sec_sum,
        duration_sec_sessions=duration_sec_sessions,
        btc_window=_normalize_btc_window(strategy_config),
        session_outcomes=tuple(session_outcomes_list),
    )


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_run_dirs(max_runs: int) -> list[Path]:
    if not RUNS_ROOT.exists():
        return []
    runs: list[tuple[float, Path]] = []
    for day_dir in RUNS_ROOT.iterdir():
        if not day_dir.is_dir():
            continue
        for run_dir in day_dir.iterdir():
            if not run_dir.is_dir():
                continue
            snap = run_dir / "strategy_snapshot.json"
            trades = run_dir / "trades.json"
            if not snap.exists() or not trades.exists():
                continue
            mt = run_dir.stat().st_mtime
            runs.append((mt, run_dir))
    runs.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in runs[: max_runs]]


def list_run_folders_detailed(max_folders: int = 200) -> dict[str, Any]:
    """
    כל תיקיות הריצה תחת RUNS_ROOT (יום/שעה) עם רשימת קבצים — לניהול ומחיקה מה-UI.
    """
    root = RUNS_ROOT.resolve()
    if not root.is_dir():
        return {
            "runs_root": str(root),
            "runs": [],
            "total_runs": 0,
            "limit_applied": max(1, max_folders),
            "truncated": False,
        }
    rows: list[tuple[float, dict[str, Any]]] = []
    for day_dir in root.iterdir():
        if not day_dir.is_dir():
            continue
        day = day_dir.name
        for run_dir in day_dir.iterdir():
            if not run_dir.is_dir():
                continue
            tim = run_dir.name
            run_key = f"{day}/{tim}"
            mt = run_dir.stat().st_mtime
            file_rows: list[dict[str, Any]] = []
            for fp in sorted(run_dir.iterdir()):
                if fp.is_file():
                    try:
                        sz = int(fp.stat().st_size)
                    except OSError:
                        sz = None
                    file_rows.append({"name": fp.name, "size_bytes": sz})
            snap = run_dir / "strategy_snapshot.json"
            trades_p = run_dir / "trades.json"
            counts = snap.is_file() and trades_p.is_file()
            trade_rows: int | None = None
            if trades_p.is_file():
                td = _load_json(trades_p)
                if isinstance(td, dict) and isinstance(td.get("trades"), list):
                    trade_rows = len(td["trades"])
            rows.append(
                (
                    mt,
                    {
                        "run_key": run_key,
                        "counts_toward_v3": counts,
                        "mtime": mt,
                        "files": file_rows,
                        "trade_rows": trade_rows,
                    },
                )
            )
    rows.sort(key=lambda x: x[0], reverse=True)
    lim = max(1, max_folders)
    total_runs = len(rows)
    truncated = total_runs > lim
    sliced = rows[:lim]
    return {
        "runs_root": str(root),
        "total_runs": total_runs,
        "limit_applied": lim,
        "truncated": truncated,
        "runs": [r[1] for r in sliced],
    }


def delete_run_folder_by_key(run_key: str) -> tuple[bool, str]:
    """מוחק תיקיית ריצה אחת (יום/שעה) — רק מתחת ל-RUNS_ROOT."""
    raw = (run_key or "").strip().replace("\\", "/")
    parts = [p for p in raw.split("/") if p]
    if len(parts) != 2:
        return False, "מפתח ריצה חייב להיות בצורה YYYY-MM-DD/HH-MM-SS"
    day, tim = parts[0], parts[1]
    if ".." in day or ".." in tim or day.startswith("/") or tim.startswith("/"):
        return False, "נתיב לא חוקי"
    root = RUNS_ROOT.resolve()
    if not root.is_dir():
        return False, "אין תיקיית ריצות"
    target = (root / day / tim).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False, "נתיב מחוץ לתיקיית הריצות"
    if not target.is_dir():
        return False, "תיקיית הריצה לא נמצאה"
    try:
        shutil.rmtree(target)
    except OSError as e:
        return False, f"שגיאה במחיקה: {e}"
    return True, "הריצה נמחקה"


# B-14: cache לאגרגט ההיסטורי הכבד (re-parse של עד 50 תיקיות × 2 JSON). מפתח = מצב תיקיות
# הריצה (count + max mtime) — *לא* עסקאות הדמו. כך עסקה חדשה לא מכריחה re-parse: היא ממוזגת
# ב-generate_tips_v2 לאחר מכן. מחזירים תמיד עותק רדוד כי generate_tips_v2 משנה את הרשימה.
_ANALYZE_RUNS_CACHE: dict[str, Any] = {"key": None, "val": None}


def analyze_runs(max_runs: int) -> list[RunStats]:
    dirs = list_run_dirs(max_runs)
    sig = (max_runs, len(dirs), max((d.stat().st_mtime for d in dirs), default=0.0))
    cached = _ANALYZE_RUNS_CACHE["val"]
    if _ANALYZE_RUNS_CACHE["key"] == sig and cached is not None:
        return list(cached)  # עותק פרטי — המיזוג ב-generate_tips_v2 לא יפגע ב-cache
    out: list[RunStats] = []
    for run_dir in dirs:
        snap = _load_json(run_dir / "strategy_snapshot.json")
        trades_data = _load_json(run_dir / "trades.json")
        if not snap or not trades_data:
            continue
        cfg = snap.get("strategy_config") or {}
        trades = trades_data.get("trades") if isinstance(trades_data.get("trades"), list) else trades_data.get("trades")  # type: ignore
        if not isinstance(trades, list):
            continue
        st = _analyze_trades(trades=trades, strategy_config=cfg)
        out.append(st)
    _ANALYZE_RUNS_CACHE["key"] = sig
    _ANALYZE_RUNS_CACHE["val"] = out
    return list(out)


def _tip_sort_key(x: dict[str, Any]) -> tuple:
    mode = x.get("tip_mode") or "full"
    mode_rank = 0 if mode == "full" else 1 if mode == "no_contrast" else 2
    m = x.get("metrics") or {}
    ex = float(m.get("expectancy") or 0.0) if isinstance(m, dict) else 0.0
    return (mode_rank, -ex)


def _generate_tips_for_window_runs(
    runs: list[RunStats],
    current_cfg: dict[str, Any],
    min_samples: int,
    use_guardrails: bool,
    window_label: str,
) -> dict[str, Any]:
    """טיפים + מדדים גלובליים לקבוצת ריצות לפי btc_window (5m/15m)."""
    param_specs = _build_param_specs()
    if not runs:
        return {
            "window_key": window_label,
            "tips": [],
            "global_metrics": None,
            "global_narrative": None,
            "summary": f"אין ריצות היסטוריות עם שוק {window_label}",
            "data_quality": {
                "runs_used": 0,
                "sessions_total": 0,
                "params_with_contrast": 0,
                "params_without_contrast": 0,
                "params_insufficient": 0,
            },
            "extended_metrics": None,
        }

    tips: list[dict[str, Any]] = []
    params_with_contrast = 0
    params_without_contrast = 0
    params_insufficient = 0

    for spec in param_specs:
        key = spec["key"]
        label = spec["label"]
        ptype = spec["type"]
        step = float(spec.get("step") or 0.0)
        requires: dict[str, Any] = spec.get("requires") or {}
        bin_aggs: dict[Any, dict[str, Any]] = {}

        for run in runs:
            inactive = False
            for req_k, req_v in requires.items():
                if run.strategy_config.get(req_k) != req_v:
                    inactive = True
                    break
            if inactive:
                continue
            cfg_val = run.strategy_config.get(key)
            if cfg_val is None:
                continue

            if ptype == "numeric":
                v = _safe_float(cfg_val)
                if v is None:
                    continue
                b = _bin_numeric(v, step=step) if step > 0 else v
            elif ptype == "bool":
                b = bool(cfg_val)
            else:
                b = str(cfg_val)

            if b not in bin_aggs:
                bin_aggs[b] = {
                    "tp_count": 0,
                    "expire_count": 0,
                    "total_count": 0,
                    "tp_sum": 0.0,
                    "expire_sum_neg": 0.0,
                    "expectancy": 0.0,
                    "win_rate": 0.0,
                    "avg_win": 0.0,
                    "avg_loss_abs": 0.0,
                    "rr": 0.0,
                    "expire_rate": 0.0,
                    "tp_peak_sum": 0.0,
                    "tp_peak_count": 0,
                    "tp_trough_sum": 0.0,
                    "tp_trough_count": 0,
                    "expire_trough_sum": 0.0,
                    "expire_trough_count": 0,
                    "after_tp_peak_delta_cents_sum": 0.0,
                    "after_tp_peak_delta_cents_count": 0,
                    "after_tp_peak_delta_pct_sum": 0.0,
                    "after_tp_peak_delta_pct_count": 0,
                    "after_tp_trough_delta_cents_sum": 0.0,
                    "after_tp_trough_delta_cents_count": 0,
                    "after_tp_trough_delta_pct_sum": 0.0,
                    "after_tp_trough_delta_pct_count": 0,
                    "entries_count_sum": 0.0,
                    "entries_count_sessions": 0,
                    "duration_sec_sum": 0.0,
                    "duration_sec_sessions": 0,
                    "session_outcomes": [],
                }
            agg = bin_aggs[b]
            agg["tp_count"] = int(agg["tp_count"]) + int(run.tp_count)
            agg["expire_count"] = int(agg["expire_count"]) + int(run.expire_count)
            agg["total_count"] = int(agg["total_count"]) + int(run.tp_count + run.expire_count)
            agg["tp_sum"] = float(agg["tp_sum"]) + float(run.tp_sum)
            agg["expire_sum_neg"] = float(agg["expire_sum_neg"]) + float(run.expire_sum_neg)
            agg["tp_peak_sum"] = float(agg["tp_peak_sum"]) + float(run.tp_peak_sum)
            agg["tp_peak_count"] = int(agg["tp_peak_count"]) + int(run.tp_peak_count)
            agg["tp_trough_sum"] = float(agg["tp_trough_sum"]) + float(run.tp_trough_sum)
            agg["tp_trough_count"] = int(agg["tp_trough_count"]) + int(run.tp_trough_count)
            agg["expire_trough_sum"] = float(agg["expire_trough_sum"]) + float(run.expire_trough_sum)
            agg["expire_trough_count"] = int(agg["expire_trough_count"]) + int(run.expire_trough_count)
            agg["after_tp_peak_delta_cents_sum"] = float(agg["after_tp_peak_delta_cents_sum"]) + float(run.after_tp_peak_delta_cents_sum)
            agg["after_tp_peak_delta_cents_count"] = int(agg["after_tp_peak_delta_cents_count"]) + int(run.after_tp_peak_delta_cents_count)
            agg["after_tp_peak_delta_pct_sum"] = float(agg["after_tp_peak_delta_pct_sum"]) + float(run.after_tp_peak_delta_pct_sum)
            agg["after_tp_peak_delta_pct_count"] = int(agg["after_tp_peak_delta_pct_count"]) + int(run.after_tp_peak_delta_pct_count)
            agg["after_tp_trough_delta_cents_sum"] = float(agg["after_tp_trough_delta_cents_sum"]) + float(run.after_tp_trough_delta_cents_sum)
            agg["after_tp_trough_delta_cents_count"] = int(agg["after_tp_trough_delta_cents_count"]) + int(run.after_tp_trough_delta_cents_count)
            agg["after_tp_trough_delta_pct_sum"] = float(agg["after_tp_trough_delta_pct_sum"]) + float(run.after_tp_trough_delta_pct_sum)
            agg["after_tp_trough_delta_pct_count"] = int(agg["after_tp_trough_delta_pct_count"]) + int(run.after_tp_trough_delta_pct_count)
            agg["entries_count_sum"] = float(agg["entries_count_sum"]) + float(run.entries_count_sum)
            agg["entries_count_sessions"] = int(agg["entries_count_sessions"]) + int(run.entries_count_sessions)
            agg["duration_sec_sum"] = float(agg["duration_sec_sum"]) + float(run.duration_sec_sum)
            agg["duration_sec_sessions"] = int(agg["duration_sec_sessions"]) + int(run.duration_sec_sessions)
            agg["session_outcomes"].extend(list(run.session_outcomes))

        for _, agg in bin_aggs.items():
            _finalize_agg_dict(agg)
            bin_outcomes: list[SessionOutcome] = agg.get("session_outcomes") or []
            bin_buckets = _build_time_buckets(bin_outcomes)
            agg["bin_time_buckets"] = bin_buckets
            agg["bin_optimal_exit_bucket"] = _optimal_exit_bucket(bin_buckets)

        current_val = current_cfg.get(key)
        recommended_val, recommended_metrics = _select_recommended_bin(
            param_key=key,
            current_val=current_val,
            bin_aggs=bin_aggs,
            min_samples=min_samples,
            use_guardrails=use_guardrails,
        )
        if recommended_val is None or recommended_metrics is None:
            params_insufficient += 1
            tips.append(
                {
                    "key": key,
                    "label": label,
                    "tip_mode": "insufficient_data",
                    "current_value": current_val,
                    "recommended_value": None,
                    "action": "לא מספיק מידע",
                    "metrics": None,
                    "reasoning": "אין מספיק מחזורים בקבוצות ההיסטוריה כדי לקבוע המלצה אמינה לתוחלת.",
                    "bin_comparison": None,
                }
            )
            continue

        action = _action_from_current(key, current_val, recommended_val)
        eligible = _eligible_bins(bin_aggs, min_samples)
        has_contrast = len(eligible) >= 2
        bin_rows = _build_bin_comparison_rows(bin_aggs, min_samples, recommended_val)

        if not has_contrast:
            params_without_contrast += 1
            short_reason = (
                "בהיסטוריה יש רק קבוצת ערכים אחת לפרמטר הזה שעומדת בסף המחזורים (או שכל הריצות היו עם אותה הגדרה). "
                "לא ניתן להשוות בין קבוצות כדי לבחור ערך אחר לפי תוחלת. "
                "כדי לקבל המלצות מבדילות לפי פרמטר — הרץ ריצות עם ערכים שונים. "
                "המדדים המפורטים מופיעים ב\"תמונת מצב כללית\" למעלה."
            )
            tips.append(
                {
                    "key": key,
                    "label": label,
                    "tip_mode": "no_contrast",
                    "current_value": current_val,
                    "recommended_value": recommended_val,
                    "action": action,
                    "metrics": None,
                    "reasoning": short_reason,
                    "bin_comparison": bin_rows if len(bin_rows) > 1 else None,
                }
            )
            continue

        params_with_contrast += 1
        exp = float(recommended_metrics.get("expectancy") or 0.0)
        win_rate = float(recommended_metrics.get("win_rate") or 0.0)
        avg_win = float(recommended_metrics.get("avg_win") or 0.0)
        avg_loss_abs = float(recommended_metrics.get("avg_loss_abs") or 0.0)
        rr = float(recommended_metrics.get("rr") or 0.0)
        total_count = int(recommended_metrics.get("total_count") or 0)
        expire_rate = float(recommended_metrics.get("expire_rate") or 0.0)
        avg_peak_roi_tp = recommended_metrics.get("avg_peak_roi_tp")
        avg_trough_roi_tp = recommended_metrics.get("avg_trough_roi_tp")
        avg_trough_roi_expire = recommended_metrics.get("avg_trough_roi_expire")
        avg_after_tp_peak_delta_cents = recommended_metrics.get("avg_after_tp_peak_delta_cents")
        avg_after_tp_peak_delta_pct = recommended_metrics.get("avg_after_tp_peak_delta_pct")
        avg_after_tp_trough_delta_cents = recommended_metrics.get("avg_after_tp_trough_delta_cents")
        avg_after_tp_trough_delta_pct = recommended_metrics.get("avg_after_tp_trough_delta_pct")
        avg_entries_per_session = recommended_metrics.get("avg_entries_per_session")
        avg_duration_sec = recommended_metrics.get("avg_duration_sec")

        expectancy_text = "חיובית" if exp > 0 else "שלילית"
        peak_hold_text = f"{float(avg_peak_roi_tp):+.1f}%" if avg_peak_roi_tp is not None else "—"
        trough_hold_text = f"{float(avg_trough_roi_tp):+.1f}%" if avg_trough_roi_tp is not None else "—"
        trough_expire_text = f"{float(avg_trough_roi_expire):+.1f}%" if avg_trough_roi_expire is not None else "—"
        after_peak_text = f"Δ{float(avg_after_tp_peak_delta_cents):+.1f}¢ ({float(avg_after_tp_peak_delta_pct):+.1f}%)" if avg_after_tp_peak_delta_cents is not None and avg_after_tp_peak_delta_pct is not None else "—"
        after_trough_text = f"Δ{float(avg_after_tp_trough_delta_cents):+.1f}¢ ({float(avg_after_tp_trough_delta_pct):+.1f}%)" if avg_after_tp_trough_delta_cents is not None and avg_after_tp_trough_delta_pct is not None else "—"
        ae = float(avg_entries_per_session) if avg_entries_per_session is not None else None
        ad = float(avg_duration_sec) if avg_duration_sec is not None else None
        entries_text = f"{ae:.1f} כניסות בממוצע" if ae is not None else "—"
        duration_text = f"{ad:.0f} שנ׳ בממוצע" if ad is not None else "—"
        reasoning = (
            f"בקבוצת {label}: מצאנו תוחלת {expectancy_text} של {exp:+.2f}$ "
            f"({win_rate:.1f}% TP win). ממוצע TP: {avg_win:+.2f}$ מול EXPIRE ממוצע (abs): -{avg_loss_abs:.2f}$. "
            f"RR={rr:.2f}, EXPIRE rate={expire_rate*100:.1f}% ובסך הכל {total_count} מחזורים."
        )
        reasoning += (
            f" עדות: שיא בזמן החזקה (מול עלות) ממוצע ב-TP: {peak_hold_text}; "
            f"שפל בזמן החזקה (מול עלות) ממוצע ב-TP: {trough_hold_text}. "
            f"שפל ב-EXPIRE (מול עלות) ממוצע: {trough_expire_text}. "
            f"אחרי TP: שיא bid {after_peak_text} ושפל bid {after_trough_text}. "
            f"({entries_text}, {duration_text})."
        )
        if exp < 0:
            reasoning += " גם אם אחוז הצלחה נראה גבוה, גודל הפסדי ה-EXPIRE מוריד את התוחלת. לכן ההמלצה היא להקטין סיכון."
        reasoning += " השוואת קבוצות (לפי תוחלת) מופיעה בטבלה בכרטיס."

        tips.append(
            {
                "key": key,
                "label": label,
                "tip_mode": "full",
                "current_value": current_val,
                "recommended_value": recommended_val,
                "action": action,
                "metrics": _metrics_payload_from_agg(recommended_metrics),
                "reasoning": reasoning,
                "bin_comparison": bin_rows,
            }
        )

    global_agg = _global_aggregate_from_runs(runs)
    g_exp = float(global_agg.get("expectancy") or 0.0)
    g_avg_win = float(global_agg.get("avg_win") or 0.0)
    g_avg_loss = float(global_agg.get("avg_loss_abs") or 0.0)
    g_total = int(global_agg.get("total_count") or 0)
    global_metrics = _metrics_payload_from_agg(global_agg)
    global_narrative = (
        f"[{window_label}] תמונת מצב על {len(runs)} ריצות ו-{g_total} מחזורים (TP/EXPIRE): "
        f"תוחלת {g_exp:+.2f}$, ממוצע TP {g_avg_win:+.2f}$, ממוצע הפסד EXPIRE (abs) {g_avg_loss:.2f}$."
    )
    summary = (
        f"[{window_label}] תוחלת {g_exp:+.2f}$ · TP ממוצע {g_avg_win:+.2f}$ · EXPIRE ממוצע (abs) {g_avg_loss:.2f}$ · "
        f"{len(runs)} ריצות"
    )

    data_quality = {
        "runs_used": len(runs),
        "sessions_total": g_total,
        "params_with_contrast": params_with_contrast,
        "params_without_contrast": params_without_contrast,
        "params_insufficient": params_insufficient,
    }

    outcomes_flat = _flatten_session_outcomes(runs)
    extended_metrics = _build_extended_metrics(outcomes_flat, min_samples)

    return {
        "window_key": window_label,
        "tips": sorted(tips, key=_tip_sort_key),
        "global_metrics": global_metrics,
        "global_narrative": global_narrative,
        "summary": summary,
        "data_quality": data_quality,
        "extended_metrics": extended_metrics,
    }


def _bin_numeric(v: float, step: float) -> float:
    if step <= 0:
        return v
    return round(v / step) * step


def _eligible_bins(bin_aggs: dict[Any, dict[str, float | int]], min_samples: int) -> list[tuple[Any, dict[str, float | int]]]:
    out: list[tuple[Any, dict[str, float | int]]] = []
    for b, m in bin_aggs.items():
        if int(m.get("total_count") or 0) >= min_samples:
            out.append((b, m))
    out.sort(key=lambda item: float(item[1].get("expectancy") or 0.0), reverse=True)
    return out


def _bin_values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 1e-9
    return a == b


def _finalize_agg_dict(agg: dict[str, Any]) -> None:
    """מעדכן agg במקום עם expectancy וממוצעי עדות (כמו בלולאת generate_tips_v2)."""
    tp_count = int(agg["tp_count"])
    expire_count = int(agg["expire_count"])
    tp_sum = float(agg["tp_sum"])
    expire_sum_neg = float(agg["expire_sum_neg"])
    ex = _compute_expectancy(tp_count, tp_sum, expire_count, expire_sum_neg)
    agg.update(ex)
    agg["expire_rate"] = float(expire_count / (tp_count + expire_count)) if (tp_count + expire_count) else 0.0

    tp_peak_count = int(agg.get("tp_peak_count") or 0)
    tp_trough_count = int(agg.get("tp_trough_count") or 0)
    expire_trough_count = int(agg.get("expire_trough_count") or 0)
    agg["avg_peak_roi_tp"] = float(agg.get("tp_peak_sum") or 0.0) / tp_peak_count if tp_peak_count else None
    agg["avg_trough_roi_tp"] = float(agg.get("tp_trough_sum") or 0.0) / tp_trough_count if tp_trough_count else None
    agg["avg_trough_roi_expire"] = float(agg.get("expire_trough_sum") or 0.0) / expire_trough_count if expire_trough_count else None

    a_peak_cc = int(agg.get("after_tp_peak_delta_cents_count") or 0)
    a_trough_cc = int(agg.get("after_tp_trough_delta_cents_count") or 0)
    agg["avg_after_tp_peak_delta_cents"] = float(agg.get("after_tp_peak_delta_cents_sum") or 0.0) / a_peak_cc if a_peak_cc else None
    agg["avg_after_tp_trough_delta_cents"] = float(agg.get("after_tp_trough_delta_cents_sum") or 0.0) / a_trough_cc if a_trough_cc else None
    a_peak_pc = int(agg.get("after_tp_peak_delta_pct_count") or 0)
    a_trough_pc = int(agg.get("after_tp_trough_delta_pct_count") or 0)
    agg["avg_after_tp_peak_delta_pct"] = float(agg.get("after_tp_peak_delta_pct_sum") or 0.0) / a_peak_pc if a_peak_pc else None
    agg["avg_after_tp_trough_delta_pct"] = float(agg.get("after_tp_trough_delta_pct_sum") or 0.0) / a_trough_pc if a_trough_pc else None
    entries_sessions = int(agg.get("entries_count_sessions") or 0)
    agg["avg_entries_per_session"] = float(agg.get("entries_count_sum") or 0.0) / entries_sessions if entries_sessions else None
    duration_sessions = int(agg.get("duration_sec_sessions") or 0)
    agg["avg_duration_sec"] = float(agg.get("duration_sec_sum") or 0.0) / duration_sessions if duration_sessions else None


def _metrics_payload_from_agg(m: dict[str, float | int]) -> dict[str, Any]:
    """מבנה metrics לטיפ (תואם UI)."""
    return {
        "expectancy": float(m.get("expectancy") or 0.0),
        "win_rate": float(m.get("win_rate") or 0.0),
        "avg_win": float(m.get("avg_win") or 0.0),
        "avg_loss_abs": float(m.get("avg_loss_abs") or 0.0),
        "rr": float(m.get("rr") or 0.0),
        "expire_rate": float(m.get("expire_rate") or 0.0),
        "total_count": int(m.get("total_count") or 0),
        "avg_peak_roi_tp": m.get("avg_peak_roi_tp"),
        "avg_trough_roi_tp": m.get("avg_trough_roi_tp"),
        "avg_trough_roi_expire": m.get("avg_trough_roi_expire"),
        "avg_after_tp_peak_delta_cents": m.get("avg_after_tp_peak_delta_cents"),
        "avg_after_tp_peak_delta_pct": m.get("avg_after_tp_peak_delta_pct"),
        "avg_after_tp_trough_delta_cents": m.get("avg_after_tp_trough_delta_cents"),
        "avg_after_tp_trough_delta_pct": m.get("avg_after_tp_trough_delta_pct"),
        "avg_entries_per_session": m.get("avg_entries_per_session"),
        "avg_duration_sec": m.get("avg_duration_sec"),
        "time_buckets": m.get("bin_time_buckets"),
        "optimal_exit_bucket": m.get("bin_optimal_exit_bucket"),
    }


def _global_aggregate_from_runs(runs: list[RunStats]) -> dict[str, float | int]:
    """מאחד את כל הריצות לאגרגט אחד (כמו בין יחיד על כל ההיסטוריה)."""
    agg: dict[str, float | int] = {
        "tp_count": 0,
        "expire_count": 0,
        "total_count": 0,
        "tp_sum": 0.0,
        "expire_sum_neg": 0.0,
        "tp_peak_sum": 0.0,
        "tp_peak_count": 0,
        "tp_trough_sum": 0.0,
        "tp_trough_count": 0,
        "expire_trough_sum": 0.0,
        "expire_trough_count": 0,
        "after_tp_peak_delta_cents_sum": 0.0,
        "after_tp_peak_delta_cents_count": 0,
        "after_tp_peak_delta_pct_sum": 0.0,
        "after_tp_peak_delta_pct_count": 0,
        "after_tp_trough_delta_cents_sum": 0.0,
        "after_tp_trough_delta_cents_count": 0,
        "after_tp_trough_delta_pct_sum": 0.0,
        "after_tp_trough_delta_pct_count": 0,
        "entries_count_sum": 0.0,
        "entries_count_sessions": 0,
        "duration_sec_sum": 0.0,
        "duration_sec_sessions": 0,
    }
    for r in runs:
        agg["tp_count"] = int(agg["tp_count"]) + int(r.tp_count)
        agg["expire_count"] = int(agg["expire_count"]) + int(r.expire_count)
        agg["total_count"] = int(agg["total_count"]) + int(r.tp_count + r.expire_count)
        agg["tp_sum"] = float(agg["tp_sum"]) + float(r.tp_sum)
        agg["expire_sum_neg"] = float(agg["expire_sum_neg"]) + float(r.expire_sum_neg)
        agg["tp_peak_sum"] = float(agg["tp_peak_sum"]) + float(r.tp_peak_sum)
        agg["tp_peak_count"] = int(agg["tp_peak_count"]) + int(r.tp_peak_count)
        agg["tp_trough_sum"] = float(agg["tp_trough_sum"]) + float(r.tp_trough_sum)
        agg["tp_trough_count"] = int(agg["tp_trough_count"]) + int(r.tp_trough_count)
        agg["expire_trough_sum"] = float(agg["expire_trough_sum"]) + float(r.expire_trough_sum)
        agg["expire_trough_count"] = int(agg["expire_trough_count"]) + int(r.expire_trough_count)
        agg["after_tp_peak_delta_cents_sum"] = float(agg["after_tp_peak_delta_cents_sum"]) + float(r.after_tp_peak_delta_cents_sum)
        agg["after_tp_peak_delta_cents_count"] = int(agg["after_tp_peak_delta_cents_count"]) + int(r.after_tp_peak_delta_cents_count)
        agg["after_tp_peak_delta_pct_sum"] = float(agg["after_tp_peak_delta_pct_sum"]) + float(r.after_tp_peak_delta_pct_sum)
        agg["after_tp_peak_delta_pct_count"] = int(agg["after_tp_peak_delta_pct_count"]) + int(r.after_tp_peak_delta_pct_count)
        agg["after_tp_trough_delta_cents_sum"] = float(agg["after_tp_trough_delta_cents_sum"]) + float(r.after_tp_trough_delta_cents_sum)
        agg["after_tp_trough_delta_cents_count"] = int(agg["after_tp_trough_delta_cents_count"]) + int(r.after_tp_trough_delta_cents_count)
        agg["after_tp_trough_delta_pct_sum"] = float(agg["after_tp_trough_delta_pct_sum"]) + float(r.after_tp_trough_delta_pct_sum)
        agg["after_tp_trough_delta_pct_count"] = int(agg["after_tp_trough_delta_pct_count"]) + int(r.after_tp_trough_delta_pct_count)
        agg["entries_count_sum"] = float(agg["entries_count_sum"]) + float(r.entries_count_sum)
        agg["entries_count_sessions"] = int(agg["entries_count_sessions"]) + int(r.entries_count_sessions)
        agg["duration_sec_sum"] = float(agg["duration_sec_sum"]) + float(r.duration_sec_sum)
        agg["duration_sec_sessions"] = int(agg["duration_sec_sessions"]) + int(r.duration_sec_sessions)
    _finalize_agg_dict(agg)
    return agg


def _build_bin_comparison_rows(
    bin_aggs: dict[Any, dict[str, float | int]],
    min_samples: int,
    recommended_val: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for b, m in bin_aggs.items():
        tc = int(m.get("total_count") or 0)
        if tc < min_samples:
            continue
        rows.append(
            {
                "bin_value": b,
                "expectancy": float(m.get("expectancy") or 0.0),
                "total_count": tc,
                "win_rate": float(m.get("win_rate") or 0.0),
                "expire_rate": float(m.get("expire_rate") or 0.0),
                "recommended": _bin_values_equal(b, recommended_val),
            }
        )
    rows.sort(key=lambda r: float(r.get("expectancy") or 0.0), reverse=True)
    return rows


def _select_recommended_bin(
    param_key: str,
    current_val: Any,
    bin_aggs: dict[Any, dict[str, float | int]],
    min_samples: int,
    use_guardrails: bool,
) -> tuple[Any | None, dict[str, float | int] | None]:
    if not bin_aggs:
        return None, None

    candidates: list[tuple[Any, dict[str, float | int]]] = []
    for b, m in bin_aggs.items():
        total_count = int(m.get("total_count") or 0)
        if total_count < min_samples:
            continue
        candidates.append((b, m))
    if not candidates:
        return None, None

    if use_guardrails and len(candidates) >= 3:
        expire_rates = [float(m.get("expire_rate") or 0.0) for _, m in candidates]
        avg_losses = [float(m.get("avg_loss_abs") or 0.0) for _, m in candidates]
        expire_rates.sort()
        avg_losses.sort()
        med_expire_rate = expire_rates[len(expire_rates) // 2]
        med_avg_loss = avg_losses[len(avg_losses) // 2]

        def is_high_risk(m: dict[str, float | int]) -> bool:
            er = float(m.get("expire_rate") or 0.0)
            al = float(m.get("avg_loss_abs") or 0.0)
            return (er > med_expire_rate + 0.15) or (al > med_avg_loss * 1.5)

        non_risky = [(b, m) for b, m in candidates if not is_high_risk(m)]
        if non_risky:
            candidates = non_risky

    candidates.sort(key=lambda item: float(item[1].get("expectancy") or 0.0), reverse=True)
    return candidates[0][0], candidates[0][1]


def _action_from_current(param_key: str, current_val: Any, recommended_val: Any) -> str:
    if current_val is None:
        return f"להגדיר ל-{recommended_val}"
    if isinstance(current_val, bool):
        if bool(current_val) == bool(recommended_val):
            return "להשאיר כפי שהוא"
        return "להפעיל" if bool(recommended_val) else "לכבות"
    if isinstance(recommended_val, bool):
        return "להחליף לפי ההמלצה"

    try:
        cur = float(current_val)
        rec = float(recommended_val)
        if abs(cur - rec) < 1e-9:
            return "להשאיר כפי שהוא"
        if rec > cur:
            return f"להגדיל ל-{recommended_val}"
        return f"להקטין ל-{recommended_val}"
    except Exception:
        if str(current_val) == str(recommended_val):
            return "להשאיר כפי שהוא"
        return f"לשנות ל-{recommended_val}"


def _build_param_specs() -> list[dict[str, Any]]:
    return [
        {"key": "take_profit_pct", "label": "יעד TP (take_profit_pct)", "type": "numeric", "step": 1.0},
        {"key": "dca_tp_override_pct", "label": "DCA Override (dca_tp_override_pct)", "type": "numeric", "step": 5.0},
        {"key": "dca_slices", "label": "מספר DCA פריסות (dca_slices)", "type": "numeric", "step": 1.0, "requires": {"dca_enabled": True}},
        {"key": "dca_interval_sec", "label": "מרווח DCA (dca_interval_sec)", "type": "numeric", "step": 5.0, "requires": {"dca_enabled": True}},
        {"key": "dca_discount_pct", "label": "הנחת DCA (dca_discount_pct)", "type": "numeric", "step": 1.0, "requires": {"dca_discount_enabled": True}},
        {"key": "hedge_enabled", "label": "גידור (hedge_enabled)", "type": "bool"},
        {"key": "hedge_combined_ask_max", "label": "hedge ask cap (hedge_combined_ask_max)", "type": "numeric", "step": 0.01, "requires": {"hedge_enabled": True}},
        {"key": "intermediate_block_new_entries", "label": "Block לאזור ביניים (intermediate_block_new_entries)", "type": "bool"},
        {"key": "min_minutes_for_entry", "label": "מינ׳ לכניסה (min_minutes_for_entry)", "type": "numeric", "step": 0.5},
        {"key": "freeze_last_minutes", "label": "קפיאה לפני סוף (freeze_last_minutes)", "type": "numeric", "step": 0.5},
        {"key": "side_preference", "label": "עדיפות צד (side_preference)", "type": "cat"},
        {"key": "auto_reenter_after_tp", "label": "ריה-כניסה אחרי TP", "type": "bool"},
        {"key": "reenter_cooldown_sec", "label": "Cooldown אחרי TP (reenter_cooldown_sec)", "type": "numeric", "step": 1.0},
    ]


def generate_tips_v2(
    max_runs: int = 50,
    min_samples: int = 50,
    use_guardrails: bool = True,
    current_cfg: dict[str, Any] | None = None,
    live_trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    all_runs = analyze_runs(max_runs=max_runs)
    # עסקאות חיות מ־demo_state תמיד עדכניות; קבצי trades.json בריצה מתעדכנים בתדירות נמוכה יותר.
    # ממזגים את הניתוח החי כדי ש«ניתוח v3» יתאים ללשונית סטטיסטיקה בלי להמתין לצילום דיסק.
    cfg_for_live = current_cfg or (all_runs[-1].strategy_config if all_runs else {}) or {}
    if live_trades:
        try:
            live_st = _analyze_trades(live_trades, cfg_for_live)
            if all_runs:
                if all_runs[0].btc_window == live_st.btc_window:
                    all_runs[0] = live_st
                else:
                    all_runs.insert(0, live_st)
            else:
                all_runs = [live_st]
        except Exception:
            pass
    if not all_runs:
        empty_window = {
            "tips": [],
            "global_metrics": None,
            "global_narrative": None,
            "summary": "אין ריצות",
            "data_quality": {
                "runs_used": 0,
                "sessions_total": 0,
                "params_with_contrast": 0,
                "params_without_contrast": 0,
                "params_insufficient": 0,
            },
            "extended_metrics": None,
        }
        return {
            "generated_at": time.time(),
            "summary": "אין מספיק ריצות היסטוריות לקרוא מהן המלצות",
            "global_metrics": None,
            "global_narrative": None,
            "data_quality": None,
            "tips": [],
            "by_btc_window": {"5m": {**empty_window, "title": "BTC Up/Down — 5 דק׳"}, "15m": {**empty_window, "title": "BTC Up/Down — 15 דק׳"}},
            "window_comparison": {"5m": _window_comparison_slice({**empty_window, "title": ""}), "15m": _window_comparison_slice({**empty_window, "title": ""})},
            "guardrails": {"min_samples": min_samples, "use_guardrails": use_guardrails},
            "note": "המדדים מחושבים לפי שוק (5m / 15m) בנפרד. המלצות מבדילות דורשות היסטוריה מספקת בכל שוק.",
        }

    current_cfg = current_cfg or all_runs[-1].strategy_config
    runs_5m = [r for r in all_runs if r.btc_window == "5m"]
    runs_15m = [r for r in all_runs if r.btc_window == "15m"]

    w5 = _generate_tips_for_window_runs(
        runs_5m,
        current_cfg,
        min_samples,
        use_guardrails,
        "BTC Up/Down — 5 דק׳",
    )
    w15 = _generate_tips_for_window_runs(
        runs_15m,
        current_cfg,
        min_samples,
        use_guardrails,
        "BTC Up/Down — 15 דק׳",
    )

    w5.pop("window_key", None)
    w15.pop("window_key", None)
    w5["title"] = "BTC Up/Down — חלון 5 דק׳"
    w15["title"] = "BTC Up/Down — חלון 15 דק׳"

    global_agg = _global_aggregate_from_runs(all_runs)
    g_exp = float(global_agg.get("expectancy") or 0.0)
    g_avg_win = float(global_agg.get("avg_win") or 0.0)
    g_avg_loss = float(global_agg.get("avg_loss_abs") or 0.0)
    g_total = int(global_agg.get("total_count") or 0)
    global_metrics = _metrics_payload_from_agg(global_agg)
    global_narrative = (
        f"סיכום כל הריצות (5m+15m): {len(all_runs)} ריצות, {g_total} מחזורים — "
        f"תוחלת {g_exp:+.2f}$, ממוצע TP {g_avg_win:+.2f}$, EXPIRE ממוצע (abs) {g_avg_loss:.2f}$. "
        f"להמלצות לפי סוג שוק — ראה כרטיסים למטה (5 דק׳ / 15 דק׳ נפרדים)."
    )
    summary = (
        f"טיפים מחושבים בנפרד לשוק 5 דק׳ ({len(runs_5m)} ריצות) ולשוק 15 דק׳ ({len(runs_15m)} ריצות). "
        f"במצטבר על כל הריצות: תוחלת {g_exp:+.2f}$, TP ממוצע {g_avg_win:+.2f}$, EXPIRE ממוצע (abs) {g_avg_loss:.2f}$."
    )

    data_quality = {
        "runs_used": len(all_runs),
        "sessions_total": g_total,
        "params_with_contrast": (w5["data_quality"]["params_with_contrast"] + w15["data_quality"]["params_with_contrast"]),
        "params_without_contrast": (w5["data_quality"]["params_without_contrast"] + w15["data_quality"]["params_without_contrast"]),
        "params_insufficient": (w5["data_quality"]["params_insufficient"] + w15["data_quality"]["params_insufficient"]),
    }

    all_tips_flat = list(w5["tips"]) + list(w15["tips"])

    return {
        "generated_at": time.time(),
        "summary": summary,
        "global_metrics": global_metrics,
        "global_narrative": global_narrative,
        "data_quality": data_quality,
        "tips": sorted(all_tips_flat, key=_tip_sort_key),
        "by_btc_window": {"5m": w5, "15m": w15},
        "window_comparison": {"5m": _window_comparison_slice(w5), "15m": _window_comparison_slice(w15)},
        "current_config_used_fallback": current_cfg,
        "guardrails": {"min_samples": min_samples, "use_guardrails": use_guardrails},
        "note": "המדדים מחושבים על בסיס sessionים (BUY→TP/EXPIRE), מופרדים לפי שוק Polymarket (5m / 15m). המלצות מבדילות לפי פרמטר דורשות לפחות שתי קבוצות ערכים שונות באותו שוק.",
    }
