"""
run_btc_variants_backtest.py — the honest backtest of the THREE extra BTC
strategy variants (mean_reversion_v1, ma_crossover_v1, donchian_breakout_v1)
vs BUY-AND-HOLD, sibling to run_btc_backtest.py.

HARMLESS half only: pure replay over REAL cached Binance history. No exchange,
no live orders, NO MARTINGALE. It reuses backtester.fetch_history's cache
(offline OK), backtester.walk_forward (tune in-sample, report on a HARD-FROZEN
~1yr OOS tail), backtester.grid_search (AUTO-COUNTED n_trials -> deflated_sharpe),
and ALWAYS prints buy_and_hold over the IDENTICAL window.

Per-variant timeframe choices (per the spec):
  * mean_reversion_v1  -> 1h AND 4h, each with the DAILY trend filter (the daily
    EMA200 gates "buy dips in an uptrend, not falling knives"). This is the
    research's most promising honest test (short-horizon BTC = mean-reversion).
  * ma_crossover_v1    -> daily AND 4h (trend following wants the slow tf).
  * donchian_breakout_v1 -> daily AND 4h.

The daily trend filter for the 1h/4h mean-reversion runs is applied by a custom
strategy_fn that reads the DAILY view's EMA200 (no look-ahead: the backtester
pre-slices each tf so only CLOSED daily bars as of the current base bar are
visible) and signals on the base tf with the variant's own per-tf trend filter
turned OFF (so the gate is the *daily* trend, not an intraday EMA200).

Run:  python3 engine/run_btc_variants_backtest.py
Writes the comparison table to engine/data/btc_variants_run.txt.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import backtester as B  # noqa: E402
import btc_strategies_extra as X  # noqa: E402
from edge_stats import deflated_sharpe  # noqa: E402

# ---- Realistic costs (identical to run_btc_backtest.py). ----
FEE_PCT = 0.05            # 0.05% per side
SLIPPAGE_PCT = 0.02       # 0.02% per fill
FUNDING = True
# Funding is charged per BAR held; scale it by bar length so a daily bar isn't
# charged the same as a 1h bar. ~0.001%/1h -> 0.024%/day (realistic perp funding).
_FUNDING_PER_HOUR = 0.001
_TF_HOURS = {"1h": 1, "4h": 4, "1d": 24}

# One frozen out-of-sample YEAR as the tail (the spec's "hard-frozen ~1yr OOS").
OOS_DAYS = 365

_INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _date(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).date()


def funding_for(base_tf: str) -> float:
    return _FUNDING_PER_HOUR * _TF_HOURS.get(base_tf, 1)


def oos_frac_for(candles, base_tf: str, days: int = OOS_DAYS) -> float:
    """Fraction of the base series whose tail spans ~`days` calendar days."""
    base = B._clean(candles[base_tf])
    n = len(base)
    if n < 3:
        return 0.3
    bars_per_day = 24 / _TF_HOURS.get(base_tf, 1)
    oos_bars = int(round(days * bars_per_day))
    oos_bars = min(max(oos_bars, 2), n - 2)
    return oos_bars / n


# ---------------------------------------------------------------------------
# History (offline-safe; reuses fetch_history's cache).
# ---------------------------------------------------------------------------

def load_history():
    out, reachable, notes = {}, True, []
    for tf, limit in (("1d", 2200), ("4h", 11000), ("1h", 28000)):
        r = B.fetch_history(interval=tf, limit=limit)
        out[tf] = r["candles"]
        reachable = reachable and r["binance_reachable"]
        notes.append(f"{tf}: source={r['source']} n={len(r['candles'])}")
    return out, reachable, "; ".join(notes)


# ---------------------------------------------------------------------------
# Strategy factories per variant.
# ---------------------------------------------------------------------------

# Bounded higher-tf lookback windows (numerically exact for every indicator
# period used; turns each per-bar call from O(full-history) into O(window)).
# Mirrors run_btc_backtest._TAIL rationale: EMA200 is fully warmed in <<400 bars.
_TAIL = {"1d": 400, "4h": 400, "1h": 400}


def make_mr_with_daily_filter(base_tf: str):
    """mean_reversion_v1 on `base_tf` gated by the DAILY EMA200 trend.

    The variant's OWN per-tf trend filter is turned OFF; instead we read the
    daily view's EMA200 and require price-vs-EMA200 agreement (long only when the
    latest daily close is above daily EMA200; short only when below). No
    look-ahead: the backtester pre-slices each tf so we only ever see daily bars
    CLOSED as of the current base bar.
    """
    def make(params: dict):
        p = dict(params or {})
        p["mr_use_trend_filter"] = False  # the DAILY gate replaces the intraday one
        p["base_tf"] = base_tf
        core = X.make_mean_reversion(p)
        trend_ema = int(p.get("mr_trend_ema", 200))

        def strat(view, i):
            sig = core(view, i)
            if sig.get("signal") not in ("long", "short"):
                return {"signal": "flat", "entry": None, "stop": None, "target": None}
            daily = (view.get("1d") or [])[-_TAIL["1d"]:]
            closes = X._closes(X._clean(daily))
            ema = X._ema_last(closes, trend_ema) if len(closes) >= trend_ema else None
            if ema is None:
                # Not enough daily history to judge the trend -> stand aside (safe).
                return {"signal": "flat", "entry": None, "stop": None, "target": None}
            dclose = closes[-1]
            side = sig["signal"]
            if side == "long" and not (dclose > ema):
                return {"signal": "flat", "entry": None, "stop": None, "target": None}
            if side == "short" and not (dclose < ema):
                return {"signal": "flat", "entry": None, "stop": None, "target": None}
            return sig
        return strat
    return make


def make_mr_plain(base_tf: str):
    """mean_reversion_v1 on `base_tf` using its OWN trend filter on that tf."""
    def make(params: dict):
        p = dict(params or {})
        p["base_tf"] = base_tf
        return X.make_mean_reversion(p)
    return make


def make_ma(base_tf: str):
    def make(params: dict):
        p = dict(params or {})
        p["base_tf"] = base_tf
        return X.make_ma_crossover(p)
    return make


def make_dc(base_tf: str):
    def make(params: dict):
        p = dict(params or {})
        p["base_tf"] = base_tf
        return X.make_donchian_breakout(p)
    return make


# ---------------------------------------------------------------------------
# The runs: (name, base_tf, make_factory, grid).
# Each grid is AUTO-COUNTED by grid_search -> n_trials for the deflated Sharpe.
# Grids are deliberately small & sensible (avoid manufacturing a lucky combo).
# ---------------------------------------------------------------------------

def build_runs():
    mr_grid = {
        "mr_rsi_oversold": [25.0, 30.0],
        "mr_rsi_overbought": [70.0, 75.0],
        "mr_atr_stop_mult": [1.5, 2.0],
        "mr_min_rr": [1.5, 2.0],
    }
    ma_grid = {
        "ma_fast": [20, 50],
        "ma_slow": [100, 200],
        "ma_atr_stop_mult": [2.0, 3.0],
        "ma_rr": [1.5, 2.0],
    }
    dc_grid = {
        "dc_period": [20, 40, 55],
        "dc_atr_stop_mult": [2.0, 3.0],
        "dc_rr": [1.5, 2.0],
    }
    return [
        ("mean_reversion_v1 (1h, daily-trend)", "1h", make_mr_with_daily_filter("1h"), mr_grid),
        ("mean_reversion_v1 (4h, daily-trend)", "4h", make_mr_with_daily_filter("4h"), mr_grid),
        ("ma_crossover_v1 (1d)", "1d", make_ma("1d"), ma_grid),
        ("ma_crossover_v1 (4h)", "4h", make_ma("4h"), ma_grid),
        ("donchian_breakout_v1 (1d)", "1d", make_dc("1d"), dc_grid),
        ("donchian_breakout_v1 (4h)", "4h", make_dc("4h"), dc_grid),
    ]


# ---------------------------------------------------------------------------
# Formatting.
# ---------------------------------------------------------------------------

def fmt_pct(x):
    try:
        return f"{x * 100:+.1f}%"
    except Exception:
        return "n/a"


def fmt(x, nd=2):
    try:
        if x == float("inf"):
            return "inf"
        return f"{x:.{nd}f}"
    except Exception:
        return "n/a"


def metric_row(label, m, dsr=None):
    pf = m.get("profit_factor", 0.0)
    pf_s = "inf" if pf == float("inf") else fmt(pf, 2)
    dsr_s = fmt(dsr, 3) if dsr is not None else "  -  "
    return (
        f"{label:<40} {fmt_pct(m.get('final_return', 0)):>9} "
        f"{fmt(m.get('sharpe', 0), 2):>7} {dsr_s:>7} "
        f"{fmt_pct(m.get('max_drawdown', 0)):>9} {pf_s:>7} "
        f"{fmt_pct(m.get('win_rate', 0)):>8} {str(m.get('n_trades', 0)):>7}"
    )


def header():
    return (
        f"{'':<40} {'Return':>9} {'Sharpe':>7} {'DSR':>7} "
        f"{'MaxDD':>9} {'PF':>7} {'Win%':>8} {'Trades':>7}"
    )


def run_variant(name, base_tf, make_factory, grid, candles_by_tf, lines):
    frac = oos_frac_for(candles_by_tf, base_tf)
    fund_rate = funding_for(base_tf)
    wf = B.walk_forward(
        make_factory, candles_by_tf, grid,
        oos_frac=frac, fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT,
        funding=FUNDING, base_tf=base_tf,
    )
    # walk_forward uses backtester's DEFAULT funding_pct_per_bar (0.001). Re-derive
    # the OOS/IS blocks with the per-tf funding so daily bars aren't undercharged:
    # we re-run the chosen params explicitly with the correct funding rate.
    best_params = wf.get("best_params", {})
    n_trials = wf.get("n_trials", 0)

    base = B._clean(candles_by_tf[base_tf])
    n = len(base)
    oos_start = max(1, min(int(round(n * (1.0 - frac))), n - 1))
    boundary_ot = B._f(base[oos_start].get("open_time"))

    # Build in-sample / OOS splits with higher-tf warm-up preserved (the per-bar
    # close-time guard in the replay still blocks any look-ahead).
    in_s, oos = {}, {}
    for tf, cs in candles_by_tf.items():
        clean = B._clean(cs)
        if tf == base_tf:
            in_s[tf] = base[:oos_start]
            oos[tf] = base[oos_start:]
        else:
            in_s[tf] = [c for c in clean if (B._f(c.get("open_time")) or 0) < boundary_ot]
            oos[tf] = clean

    strat = make_factory(best_params)
    is_bt = B.backtest(strat, in_s, fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT,
                       funding=FUNDING, funding_pct_per_bar=fund_rate,
                       config=best_params, base_tf=base_tf)
    oos_bt = B.backtest(strat, oos, fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT,
                        funding=FUNDING, funding_pct_per_bar=fund_rate,
                        config=best_params, base_tf=base_tf)

    is_s, is_bh = is_bt["strategy"], is_bt["buy_and_hold"]
    oos_s, oos_bh = oos_bt["strategy"], oos_bt["buy_and_hold"]
    is_dsr = deflated_sharpe(is_s.get("returns", []), n_trials) if is_s.get("returns") else 0.0
    oos_dsr = deflated_sharpe(oos_s.get("returns", []), n_trials) if oos_s.get("returns") else 0.0

    lines.append("")
    lines.append("=" * 118)
    lines.append(f"{name}  [base_tf={base_tf}]")
    lines.append(
        f"  window: {_date(base[0]['open_time'])}..{_date(base[-1]['open_time'])} "
        f"({n} bars); OOS tail = last {n - oos_start} bars "
        f"({_date(base[oos_start]['open_time'])}..{_date(base[-1]['open_time'])}, FROZEN ~1yr)"
    )
    lines.append(f"  auto-counted n_trials = {n_trials}; best in-sample params = {best_params}")
    lines.append(f"  funding/bar = {fund_rate}%  (fee {FEE_PCT}%/side, slippage {SLIPPAGE_PCT}%/fill)")
    lines.append("-" * 118)
    lines.append(header())
    lines.append("IN-SAMPLE")
    lines.append(metric_row("  " + name, is_s, is_dsr))
    lines.append(metric_row("  buy-and-hold", is_bh, None))
    lines.append("OUT-OF-SAMPLE (frozen)")
    lines.append(metric_row("  " + name, oos_s, oos_dsr))
    lines.append(metric_row("  buy-and-hold", oos_bh, None))

    return {
        "name": name, "base_tf": base_tf, "n_trials": n_trials,
        "best_params": best_params, "oos_bars": n - oos_start,
        "in_sample": {"strategy": _slim(is_s), "buy_and_hold": _slim(is_bh), "deflated_sharpe": is_dsr},
        "oos": {"strategy": _slim(oos_s), "buy_and_hold": _slim(oos_bh), "deflated_sharpe": oos_dsr},
    }


def _slim(m):
    """Metrics without the (huge) per-trade list, for JSON artifacts."""
    pf = m.get("profit_factor", 0.0)
    return {
        "final_return": m.get("final_return", 0.0),
        "sharpe": m.get("sharpe", 0.0),
        "max_drawdown": m.get("max_drawdown", 0.0),
        "profit_factor": (None if pf == float("inf") else pf),
        "win_rate": m.get("win_rate", 0.0),
        "n_trades": m.get("n_trades", 0),
        "avg_R": m.get("avg_R", 0.0),
    }


def verdict(summary, lines):
    lines.append("")
    lines.append("=" * 118)
    lines.append("HONEST VERDICT (out-of-sample, after fees + slippage + funding; multiple-testing corrected)")
    lines.append("-" * 118)
    winners = []
    for r in summary:
        oos_s = r["oos"]["strategy"]
        oos_bh = r["oos"]["buy_and_hold"]
        dsr = r["oos"]["deflated_sharpe"]
        pf = oos_s.get("profit_factor")
        n_tr = oos_s.get("n_trades", 0)
        s_ret = oos_s.get("final_return", 0.0)
        bh_ret = oos_bh.get("final_return", 0.0)
        s_sh = oos_s.get("sharpe", 0.0)
        bh_sh = oos_bh.get("sharpe", 0.0)
        # A REAL edge: beats B&H on return AND risk-adjusted, PF>1, DSR>0, and a
        # non-trivial trade count (>=30) so a fluke regime is visible.
        pf_ok = (pf is None) or (pf > 1.0)  # None == inf PF (all wins) -> ok
        real = (s_ret > bh_ret and s_sh > bh_sh and pf_ok and dsr > 0.0 and n_tr >= 30)
        if real:
            winners.append(r)
        flags = []
        if n_tr < 30:
            flags.append(f"only {n_tr} OOS trades (<30 -> a fluke could hide here)")
        if not (s_ret > bh_ret):
            flags.append(f"return {fmt_pct(s_ret)} <= B&H {fmt_pct(bh_ret)}")
        if not (s_sh > bh_sh):
            flags.append(f"Sharpe {fmt(s_sh)} <= B&H {fmt(bh_sh)}")
        if not pf_ok:
            flags.append(f"PF {fmt(pf or 0)} <= 1")
        if not (dsr > 0.0):
            flags.append(f"DSR {fmt(dsr, 3)} == 0 (no edge after {r['n_trials']} trials)")
        verdict_word = "REAL EDGE?" if real else "no edge"
        lines.append(
            f"  {r['name']:<40} -> {verdict_word}  "
            f"[ret {fmt_pct(s_ret)} vs B&H {fmt_pct(bh_ret)}, "
            f"Sharpe {fmt(s_sh)} vs {fmt(bh_sh)}, "
            f"PF {('inf' if pf is None else fmt(pf))}, DSR {fmt(dsr, 3)}, "
            f"{n_tr} trades]"
        )
        if flags:
            lines.append(f"      why not: {'; '.join(flags)}")
    lines.append("-" * 118)
    if not winners:
        lines.append(
            "NONE of the three variants beats risk-adjusted buy-and-hold OUT-OF-SAMPLE "
            "after fees with Deflated-Sharpe > 0."
        )
        lines.append(
            "No variant clears PF>1 AND DSR>0 AND n_trades>=30 on the frozen OOS it was "
            "never tuned on."
        )
        lines.append("==> hold BTC remains the honest answer.")
    else:
        lines.append(
            f"{len(winners)} variant(s) PASS the full bar OOS (PF>1, DSR>0, n_trades>=30, "
            f"beats B&H on return AND Sharpe):"
        )
        for r in winners:
            lines.append(f"  * {r['name']}")
        lines.append(
            "SCRUTINY REQUIRED before believing it: one OOS window can still be a single "
            "lucky regime. Confirm trade count, profit_factor, and regime-stability above."
        )


def main():
    lines = []
    lines.append("=" * 118)
    lines.append("BTC strategy VARIANTS — REAL-history backtest vs BUY-AND-HOLD "
                 "(HARMLESS half; no live code; NO MARTINGALE)")
    lines.append("=" * 118)

    candles_by_tf, reachable, note = load_history()
    lines.append(f"History: {note}")
    if not reachable:
        lines.append("")
        lines.append("*** NOTE: Binance not reached live here; using the CACHED REAL history "
                     "(offline-safe). ***")
        lines.append("*** The caches were produced from real Binance pulls; numbers below are "
                     "on real BTC OHLCV. ***")

    runs = build_runs()
    lines.append(f"Variants x timeframes: {len(runs)} runs; "
                 f"costs fee {FEE_PCT}%/side + slippage {SLIPPAGE_PCT}%/fill + funding (per-tf).")

    summary = []
    for name, base_tf, make_factory, grid in runs:
        r = run_variant(name, base_tf, make_factory, grid, candles_by_tf, lines)
        summary.append(r)

    verdict(summary, lines)

    text = "\n".join(lines) + "\n"
    print(text)

    out_txt = os.path.join(_HERE, "data", "btc_variants_run.txt")
    with open(out_txt, "w") as fh:
        fh.write(text)
    out_json = os.path.join(_HERE, "data", "btc_variants_run.json")
    with open(out_json, "w") as fh:
        json.dump({
            "history": note, "binance_reachable": reachable,
            "costs": {"fee_pct": FEE_PCT, "slippage_pct": SLIPPAGE_PCT,
                      "funding": FUNDING, "funding_per_hour_pct": _FUNDING_PER_HOUR},
            "oos_days": OOS_DAYS, "runs": summary,
        }, fh, indent=2)
    print(f"\nWrote {out_txt}\nWrote {out_json}")


if __name__ == "__main__":
    main()
