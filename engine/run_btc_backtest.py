"""
run_btc_backtest.py — the honest BTC TA backtest run.

HARMLESS half only: pure replay over REAL cached Binance history. No exchange,
no live orders, no martingale. This wires `btc_strategy.evaluate_signal`
(trend_pullback_v1) into `backtester.backtest` with realistic costs
(0.05%/side fees + slippage + perpetual funding), runs a walk-forward (tune
in-sample, report on a hard-frozen OOS tail), and prints a clear, honest table
of the strategy vs BUY-AND-HOLD for BOTH in-sample and out-of-sample.

It reuses the no-look-ahead replay engine: the multi-timeframe view is sliced so
the strategy can NEVER see a higher-tf bar that closes after the current 1h base
bar (the `_slice_len` close-time guard). Higher timeframes (daily/4h) are passed
WHOLE so their indicators (daily EMA200 etc.) have warm-up; only the 1h base bar
window decides when a trade may fill, and fills happen on the NEXT 1h open.

Run:  python3 engine/run_btc_backtest.py
(Needs the engine/data/btc_{1d,4h,1h}.json caches; will fetch live if missing
 and Binance is reachable, otherwise prints the honest "must re-run" note.)
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import backtester as B  # noqa: E402
import btc_strategy as S  # noqa: E402
from edge_stats import deflated_sharpe  # noqa: E402

FEE_PCT = 0.05            # 0.05% per side (taker-ish, realistic)
SLIPPAGE_PCT = 0.02       # 0.02% per fill
FUNDING = True
FUNDING_PCT_PER_BAR = 0.001  # ~0.001%/1h bar ~= 0.024%/day, realistic perp funding
BASE_TF = "1h"
OOS_FRAC = 0.30           # last ~30% of the 1h window is the frozen OOS tail


def _date(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).date()


# Bounded higher-tf lookback windows. EMAs/RSI/ATR converge within a few
# multiples of their period; slicing to a recent tail before each call is both
# numerically equivalent (EMA200 is fully warmed in <<400 bars) AND turns the
# per-bar cost from O(full-history) into O(window) — the difference between a
# minute and an hour over ~14k base bars * 16 grid combos. NO look-ahead is
# introduced: we only ever DROP old bars, never add future ones.
# 1d=800 makes EMA200 numerically exact (rel-err ~9e-5 vs full history) while
# still ~2.7x cheaper than the full ~2200-bar daily series; 4h/1h windows are
# far longer than any indicator period used, so they're exact too.
_TAIL = {"1d": 800, "4h": 300, "1h": 60}


def make_strategy(params: dict):
    """Build a backtester strategy_fn from a btc_strategy config combo.

    The backtester hands us a per-tf VIEW already sliced for no-look-ahead; we
    just route daily/4h/1h into evaluate_signal. Returns a flat signal dict the
    backtester + risk_engine understand.
    """
    cfg = dict(params) if isinstance(params, dict) else {}

    def strat(view, i):
        daily = (view.get("1d") or [])[-_TAIL["1d"]:]
        h4 = (view.get("4h") or [])[-_TAIL["4h"]:]
        h1 = (view.get("1h") or [])[-_TAIL["1h"]:]
        sig = S.evaluate_signal(daily, h4, h1, config=cfg)
        side = sig.get("signal")
        if side not in ("long", "short"):
            return {"signal": "flat", "entry": None, "stop": None, "target": None}
        return {
            "signal": side,
            "entry": sig.get("entry"),
            "stop": sig.get("stop"),
            "target": sig.get("target"),
        }

    return strat


def load_history():
    """Load (or fetch+cache) daily/4h/1h. Returns (candles_by_tf, reachable, note)."""
    out = {}
    reachable = True
    notes = []
    for tf, limit in (("1d", 2200), ("4h", 11000), ("1h", 28000)):
        r = B.fetch_history(interval=tf, limit=limit)
        out[tf] = r["candles"]
        reachable = reachable and r["binance_reachable"]
        notes.append(f"{tf}: source={r['source']} n={len(r['candles'])}")
    return out, reachable, "; ".join(notes)


# ---------------------------------------------------------------------------
# OOS run WITH higher-tf warm-up (only the base window is hard-cut).
# ---------------------------------------------------------------------------

def _oos_boundary(base, oos_frac):
    n = len(base)
    frac = min(max(oos_frac, 0.05), 0.9)
    idx = max(1, min(int(round(n * (1.0 - frac))), n - 1))
    return idx


def split_base_window(candles_by_tf, oos_frac):
    """In-sample: base 1h truncated at the boundary, higher TFs truncated at the
    same wall-clock boundary (no future leak). OOS: base 1h is ONLY the tail, but
    higher TFs are passed WHOLE so daily/4h indicators have warm-up — the
    close-time look-ahead guard in the replay still hides any future bar."""
    base = B._clean(candles_by_tf[BASE_TF])
    idx = _oos_boundary(base, oos_frac)
    boundary_ot = B._f(base[idx].get("open_time"))

    in_sample = {}
    oos = {}
    for tf, cs in candles_by_tf.items():
        clean = B._clean(cs)
        if tf == BASE_TF:
            in_sample[tf] = base[:idx]
            oos[tf] = base[idx:]
            continue
        # Higher TFs: in-sample sees only bars opening before the boundary;
        # OOS sees the WHOLE series (the per-bar guard prevents look-ahead).
        ins = [c for c in clean if (B._f(c.get("open_time")) or 0) < boundary_ot]
        in_sample[tf] = ins
        oos[tf] = clean
    return in_sample, oos, idx, boundary_ot


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
        f"{label:<26} {fmt_pct(m.get('final_return',0)):>9} "
        f"{fmt(m.get('sharpe',0),2):>7} {dsr_s:>7} "
        f"{fmt_pct(m.get('max_drawdown',0)):>9} {pf_s:>7} "
        f"{fmt_pct(m.get('win_rate',0)):>8} {str(m.get('n_trades',0)):>7} "
        f"{fmt(m.get('avg_R',0),2):>7}"
    )


def header():
    return (
        f"{'':<26} {'Return':>9} {'Sharpe':>7} {'DSR':>7} "
        f"{'MaxDD':>9} {'PF':>7} {'Win%':>8} {'Trades':>7} {'avgR':>7}"
    )


def main():
    print("=" * 110)
    print("BTC trend_pullback_v1 — REAL-history backtest vs BUY-AND-HOLD (HARMLESS half; no live code)")
    print("=" * 110)

    candles_by_tf, reachable, note = load_history()
    print(f"History: {note}")
    if not reachable:
        print("\n*** WARNING: Binance was NOT reachable for at least one timeframe. ***")
        print("*** Numbers below are from cache/synthetic and are NOT authoritative.   ***")
        print("*** The real run MUST execute where Binance is reachable (Railway).     ***\n")

    base = B._clean(candles_by_tf[BASE_TF])
    print(f"Base ({BASE_TF}): {len(base)} bars, "
          f"{_date(base[0]['open_time'])} .. {_date(base[-1]['open_time'])}")
    print(f"Costs: fee {FEE_PCT}%/side, slippage {SLIPPAGE_PCT}%/fill, "
          f"funding {'on' if FUNDING else 'off'} ({FUNDING_PCT_PER_BAR}%/1h bar)")

    # ---- The tuning grid. Each combo is AUTO-COUNTED as an n_trials burden. ----
    grid = {
        "trigger_body_k": [0.3, 0.5],
        "rsi_pullback_lo": [35.0, 40.0],
        "pullback_band_atr": [1.0, 1.5],
        "use_liquidity_gate": [True, False],
    }
    n_combos = 1
    for v in grid.values():
        n_combos *= len(v)
    print(f"Grid: {n_combos} combos (auto-counted n_trials for the deflated Sharpe)\n")

    in_sample, oos, oos_idx, boundary_ot = split_base_window(candles_by_tf, OOS_FRAC)
    print(f"Walk-forward split @ 1h index {oos_idx}: "
          f"in-sample {_date(base[0]['open_time'])}..{_date(base[oos_idx-1]['open_time'])} "
          f"({oos_idx} bars), "
          f"OOS {_date(base[oos_idx]['open_time'])}..{_date(base[-1]['open_time'])} "
          f"({len(base)-oos_idx} bars, FROZEN)\n")

    # 1) TUNE on in-sample only — grid_search auto-counts n_trials.
    gs = B.grid_search(make_strategy, in_sample, grid,
                       fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT,
                       funding=FUNDING, base_tf=BASE_TF)
    n_trials = gs["n_trials"]
    best = gs["best"]
    best_params = best["params"] if best else {}
    is_strat = best["strategy"] if best else B._empty_metrics()
    is_bh = best["buy_and_hold"] if best else B._empty_metrics()
    is_dsr = best["deflated_sharpe"] if best else 0.0

    # 2) Run the CHOSEN params ONCE on the frozen OOS tail (higher-tf warm-up kept).
    oos_strat = make_strategy(best_params)
    oos_bt = B.backtest(oos_strat, oos, fee_pct=FEE_PCT, slippage_pct=SLIPPAGE_PCT,
                        funding=FUNDING, config=best_params, base_tf=BASE_TF)
    oos_s = oos_bt["strategy"]
    oos_bh = oos_bt["buy_and_hold"]
    oos_returns = oos_s.get("returns", [])
    oos_dsr = deflated_sharpe(oos_returns, n_trials) if oos_returns else 0.0

    print(f"AUTO-COUNTED n_trials (multiple-testing burden): {n_trials}")
    print(f"Best in-sample params: {best_params}\n")

    print(header())
    print("-" * 110)
    print("IN-SAMPLE")
    print(metric_row("  trend_pullback_v1", is_strat, is_dsr))
    print(metric_row("  buy-and-hold", is_bh, None))
    print("-" * 110)
    print("OUT-OF-SAMPLE (frozen)")
    print(metric_row("  trend_pullback_v1", oos_s, oos_dsr))
    print(metric_row("  buy-and-hold", oos_bh, None))
    print("=" * 110)

    verdict(oos_s, oos_bh, oos_dsr, n_trials, reachable)


def verdict(oos_s, oos_bh, oos_dsr, n_trials, reachable):
    print("\nHONEST VERDICT (out-of-sample, after fees + slippage + funding)")
    print("-" * 110)
    if not reachable:
        print("Binance unreachable here -> numbers are NOT authoritative; re-run on Railway.")
        return
    s_ret = oos_s.get("final_return", 0.0)
    bh_ret = oos_bh.get("final_return", 0.0)
    s_sh = oos_s.get("sharpe", 0.0)
    bh_sh = oos_bh.get("sharpe", 0.0)
    n_tr = oos_s.get("n_trades", 0)
    beats_ret = s_ret > bh_ret
    beats_sharpe = s_sh > bh_sh
    dsr_strong = oos_dsr >= 0.95
    enough_trades = n_tr >= 30
    if beats_ret and beats_sharpe and dsr_strong and enough_trades:
        print(f"trend_pullback_v1 BEATS risk-adjusted buy-and-hold OOS "
              f"(return {fmt_pct(s_ret)} vs {fmt_pct(bh_ret)}, Sharpe {fmt(s_sh)} vs "
              f"{fmt(bh_sh)}, DSR {fmt(oos_dsr,3)} with n_trials={n_trials}, {n_tr} trades).")
        print("Even so: this is ONE OOS window. Do NOT enable live without the full "
              "gate (eff-N>=400, FDR+DSR survivor, regime-stable).")
    else:
        print("trend_pullback_v1 does NOT clear the bar OOS. The honest answer is HOLD BTC.")
        reasons = []
        if not beats_ret:
            reasons.append(f"return {fmt_pct(s_ret)} <= buy-and-hold {fmt_pct(bh_ret)}")
        if not beats_sharpe:
            reasons.append(f"Sharpe {fmt(s_sh)} <= buy-and-hold {fmt(bh_sh)}")
        if not dsr_strong:
            reasons.append(f"deflated Sharpe {fmt(oos_dsr,3)} < 0.95 (could be luck after "
                           f"{n_trials} trials)")
        if not enough_trades:
            reasons.append(f"only {n_tr} OOS trades (< 30; not enough to trust)")
        print("Why: " + "; ".join(reasons) + ".")
        print('Per the spec, "don\'t trade" is the SUCCESSFUL outcome of this tool, '
              "not a failure.")


if __name__ == "__main__":
    main()
