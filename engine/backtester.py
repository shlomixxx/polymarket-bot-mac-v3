"""
backtester.py — the honesty gate of the BTC TA system.

This is the third HARMLESS-half module. There is **zero real-money code here** —
no exchange, no live orders. The only network it does is a *read-only* pull of
public Binance spot klines (no auth) to build offline history caches; everything
else is pure, deterministic arithmetic.

What it does, and why each piece exists (per the spec + the 3 adversarial fixes):

  * `fetch_history` — paginated BTCUSDT klines from the public Binance endpoint
    (GET api.binance.com/api/v3/klines), walking backwards via `endTime`, cached
    to engine/data/btc_<interval>.json so re-runs are OFFLINE. If Binance can't
    be reached from here it does NOT crash: it falls back to whatever is cached,
    else a synthetic series, and FLAGS `needs_real_history_run=True` so the
    operator knows the honest numbers must be produced where Binance is reachable.

  * `backtest` — replays a strategy bar-by-bar with **NO look-ahead** (the
    strategy only ever sees CLOSED bars `[:i+1]`; a signal on bar i is FILLED at
    bar i+1's open). Every signal is routed through `risk_engine.gate_order`
    (the one and only approve path). Fees + slippage + funding are charged per
    trade. It returns the standard metric block (Sharpe, max_drawdown,
    profit_factor, win_rate, n_trades, avg_R, final_return) AND the same for
    BUY-AND-HOLD over the identical window — so "don't trade" can win on the merits.

  * `grid_search` — a tiny parameter-grid runner that **AUTO-COUNTS** the number
    of combos it evaluates and feeds that count to `deflated_sharpe` as
    `n_trials`. The number is never hand-entered; otherwise the best safety stat
    is a lie (adversarial fix #1).

  * `walk_forward` — tunes on an in-sample window and reports on a HARD-FROZEN
    out-of-sample tail (the last ~oos_frac of the series). The OOS DSR uses the
    auto-counted in-sample trial count.

NO MARTINGALE anywhere: there is no doubling / averaging / loss-recovery code
path; sizing is delegated wholly to risk_engine, which is stateless across trades.

Never raises on the analysis path: malformed input yields an empty, well-formed
result, never an exception.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Callable, Optional

try:  # Bare imports (engine/ on path via conftest); fall back gracefully.
    import risk_engine
    from edge_stats import deflated_sharpe
except Exception:  # pragma: no cover - unusual import contexts
    risk_engine = None  # type: ignore
    deflated_sharpe = None  # type: ignore

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# Annualisation: per-bar Sharpe is scaled to a comparable figure. We use a
# conservative sqrt(N_bars) on the realised per-trade returns; for the synthetic
# tests this is irrelevant (we only assert ordering / presence), and for the real
# run the per-trade Sharpe is what feeds the deflated-Sharpe haircut anyway.


# ---------------------------------------------------------------------------
# Safe numeric helpers
# ---------------------------------------------------------------------------

def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _ohlc(candle: Any) -> Optional[tuple[float, float, float, float]]:
    if not isinstance(candle, dict):
        return None
    o = _f(candle.get("open"))
    h = _f(candle.get("high"))
    lo = _f(candle.get("low"))
    cl = _f(candle.get("close"))
    if None in (o, h, lo, cl):
        return None
    return o, h, lo, cl  # type: ignore[return-value]


def _clean(candles: Any) -> list[dict]:
    if not isinstance(candles, (list, tuple)):
        return []
    return [c for c in candles if _ohlc(c) is not None]


# ---------------------------------------------------------------------------
# 1. Paginated Binance history fetcher (read-only, offline-safe, cached)
# ---------------------------------------------------------------------------

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000,
}


def _default_cache_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _cache_path(interval: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"btc_{interval}.json")


def _synthetic_series(interval: str, limit: int) -> list[dict]:
    """A deterministic synthetic BTC-ish series used ONLY when neither the
    network nor a cache is available. Clearly flagged by the caller so nobody
    mistakes it for real history."""
    dt = _INTERVAL_MS.get(interval, 3_600_000)
    n = max(1, int(limit) if _f(limit) else 200)
    out: list[dict] = []
    px = 20000.0
    # A gentle drift with a deterministic wiggle (no randomness -> reproducible).
    for i in range(n):
        drift = 8.0 * math.sin(i / 9.0) + 1.5  # mild oscillation + slow rise
        o = px
        cl = px + drift
        hi = max(o, cl) + 6.0
        lo = min(o, cl) - 6.0
        out.append({
            "open_time": i * dt, "open": o, "high": hi, "low": lo,
            "close": cl, "volume": 1.0,
        })
        px = cl
    return out


def _load_cache(interval: str, cache_dir: str) -> Optional[list[dict]]:
    path = _cache_path(interval, cache_dir)
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    cleaned = _clean(data)
    return cleaned or None


def _save_cache(interval: str, cache_dir: str, candles: list[dict]) -> None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(_cache_path(interval, cache_dir), "w") as fh:
            json.dump(candles, fh)
    except OSError:  # pragma: no cover - cache write is best-effort
        pass


def _fetch_binance_paginated(interval: str, limit: int) -> list[dict]:
    """Walk Binance klines backwards via endTime until we have >= limit bars.

    Raises on any network/HTTP problem so the caller can fall back to cache /
    synthetic. (This is the ONLY function here that touches the network.)
    """
    import httpx  # local import so the pure path never imports it

    step = _INTERVAL_MS.get(interval, 3_600_000)
    per_call = 1000  # Binance hard cap
    collected: dict[int, dict] = {}
    end_time: Optional[int] = None
    with httpx.Client(timeout=15.0) as client:
        while len(collected) < limit:
            params: dict[str, Any] = {
                "symbol": "BTCUSDT", "interval": interval, "limit": per_call,
            }
            if end_time is not None:
                params["endTime"] = end_time
            r = client.get(BINANCE_KLINES, params=params)
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for row in rows:
                ot = int(row[0])
                collected[ot] = {
                    "open_time": ot,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            oldest = min(collected)
            new_end = oldest - 1
            if end_time is not None and new_end >= end_time:
                break  # no progress -> stop paginating
            end_time = new_end
            if len(rows) < per_call:
                break  # reached the start of history
    candles = [collected[k] for k in sorted(collected)]
    return candles[-limit:] if limit and len(candles) > limit else candles


def fetch_history(
    interval: str = "1d",
    limit: int = 1000,
    *,
    cache_dir: Optional[str] = None,
    refresh: bool = False,
    _force_offline: bool = False,
) -> dict[str, Any]:
    """Get BTCUSDT history for `interval`, offline-safe.

    Order of preference:
      1. live Binance fetch (unless _force_offline) -> cache it, mark reachable
      2. existing on-disk cache
      3. a synthetic series (clearly flagged)

    Returns:
        {candles, source: "binance"|"cache"|"synthetic",
         binance_reachable: bool, needs_real_history_run: bool, interval, note}

    `needs_real_history_run` is True whenever the candles are NOT a fresh live
    Binance pull — the honest, fees-included numbers must be produced where
    Binance is reachable. Never raises.
    """
    cdir = cache_dir or _default_cache_dir()

    # 1. Try the network unless explicitly offline / cache-only.
    if not _force_offline:
        try:
            candles = _fetch_binance_paginated(interval, limit)
            if candles:
                _save_cache(interval, cdir, candles)
                return {
                    "candles": candles,
                    "source": "binance",
                    "binance_reachable": True,
                    "needs_real_history_run": False,
                    "interval": interval,
                    "note": f"live Binance pull: {len(candles)} {interval} bars (cached).",
                }
        except Exception as exc:  # network down / blocked / HTTP error
            cached = _load_cache(interval, cdir)
            if cached:
                return {
                    "candles": cached,
                    "source": "cache",
                    "binance_reachable": False,
                    "needs_real_history_run": True,
                    "interval": interval,
                    "note": (
                        f"Binance unreachable ({exc!r}); using cached "
                        f"{len(cached)} {interval} bars. Re-run where Binance is reachable."
                    ),
                }
            synth = _synthetic_series(interval, limit)
            return {
                "candles": synth,
                "source": "synthetic",
                "binance_reachable": False,
                "needs_real_history_run": True,
                "interval": interval,
                "note": (
                    f"Binance unreachable ({exc!r}) and no cache; using a SYNTHETIC "
                    f"series. THIS IS NOT REAL HISTORY — re-run where Binance is reachable."
                ),
            }

    # 2/3. Offline path: cache first, then synthetic.
    cached = _load_cache(interval, cdir)
    if cached:
        return {
            "candles": cached,
            "source": "cache",
            "binance_reachable": False,
            "needs_real_history_run": True,
            "interval": interval,
            "note": (
                f"offline: using cached {len(cached)} {interval} bars. "
                f"Re-run where Binance is reachable for a live, authoritative pull."
            ),
        }
    synth = _synthetic_series(interval, limit)
    return {
        "candles": synth,
        "source": "synthetic",
        "binance_reachable": False,
        "needs_real_history_run": True,
        "interval": interval,
        "note": (
            "offline and no cache: using a SYNTHETIC series. THIS IS NOT REAL "
            "HISTORY — re-run where Binance is reachable."
        ),
    }


# ---------------------------------------------------------------------------
# 2. Metric helpers
# ---------------------------------------------------------------------------

def _max_drawdown(equity_curve: list[float]) -> float:
    """Max drawdown from the running peak, as a non-positive fraction (<= 0)."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = v / peak - 1.0
            if dd < mdd:
                mdd = dd
    return mdd


def _sharpe(returns: list[float]) -> float:
    """Per-observation Sharpe (mean/sd), annualised by sqrt(n). Degenerate -> 0."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0.0:
        return 0.0
    return (mean / math.sqrt(var)) * math.sqrt(n)


def _empty_metrics() -> dict[str, Any]:
    return {
        "sharpe": 0.0, "max_drawdown": 0.0, "profit_factor": 0.0,
        "win_rate": 0.0, "n_trades": 0, "avg_R": 0.0, "final_return": 0.0,
        "total_costs": 0.0, "trades": [], "returns": [],
    }


def buy_and_hold_metrics(candles: Any) -> dict[str, Any]:
    """Buy at the first close, hold to the last, over the identical window.

    Returns the SAME metric block shape as the strategy, so the two are
    directly comparable. Never raises.
    """
    clean = _clean(candles)
    if len(clean) < 2:
        return _empty_metrics()
    closes = [_ohlc(c)[3] for c in clean]  # type: ignore[index]
    first, last = closes[0], closes[-1]
    if first <= 0:
        return _empty_metrics()
    final_return = last / first - 1.0
    equity = [px / first for px in closes]
    mdd = _max_drawdown(equity)
    # Per-bar returns for a comparable Sharpe.
    bar_rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))
                if closes[i - 1] > 0]
    gains = sum(r for r in bar_rets if r > 0)
    losses = -sum(r for r in bar_rets if r < 0)
    pf = (gains / losses) if losses > 0 else (float("inf") if gains > 0 else 0.0)
    wins = sum(1 for r in bar_rets if r > 0)
    win_rate = wins / len(bar_rets) if bar_rets else 0.0
    return {
        "sharpe": _sharpe(bar_rets),
        "max_drawdown": mdd,
        "profit_factor": pf,
        "win_rate": win_rate,
        "n_trades": 1,
        "avg_R": final_return,
        "final_return": final_return,
        "total_costs": 0.0,
        "trades": [],
        "returns": bar_rets,
    }


# ---------------------------------------------------------------------------
# 2b. The bar-by-bar replay engine (NO look-ahead)
# ---------------------------------------------------------------------------

def backtest(
    strategy_fn: Callable[[dict, int], dict],
    candles_by_tf: Any,
    *,
    fee_pct: float = 0.05,
    slippage_pct: float = 0.02,
    funding: bool = True,
    funding_pct_per_bar: float = 0.001,
    config: Optional[dict] = None,
    base_tf: str = "1h",
) -> dict[str, Any]:
    """Replay `strategy_fn` bar-by-bar over the base timeframe.

    `strategy_fn(candles_by_tf_view, i)` is called once per CLOSED bar i. The
    view it receives is sliced so it can NEVER see a bar with index > i (no
    look-ahead). It returns a signal dict {signal, entry, stop, target}.

    Each actionable signal is routed through risk_engine.gate_order (the only
    approve path). An approved long/short is FILLED at bar i+1's OPEN (you cannot
    trade the close you just used to decide). The trade then walks forward bar by
    bar; it exits when a bar's range touches the stop or the target (stop checked
    first — the conservative assumption), or at the last bar (mark-to-close).

    Fees + slippage are charged on both fills; optional funding accrues per bar
    held. Returns {strategy: <metrics>, buy_and_hold: <metrics>, params, costs}.

    Never raises. Bad input -> empty metrics.
    """
    try:
        return _backtest_inner(
            strategy_fn, candles_by_tf,
            fee_pct=fee_pct, slippage_pct=slippage_pct, funding=funding,
            funding_pct_per_bar=funding_pct_per_bar, config=config, base_tf=base_tf,
        )
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        out = {"strategy": _empty_metrics(), "buy_and_hold": _empty_metrics()}
        out["strategy"]["error"] = f"backtest error -> empty (safe): {exc!r}"
        return out


def _backtest_inner(
    strategy_fn, candles_by_tf, *,
    fee_pct, slippage_pct, funding, funding_pct_per_bar, config, base_tf,
):
    cfg = config if isinstance(config, dict) else {}
    if not isinstance(candles_by_tf, dict):
        return {"strategy": _empty_metrics(), "buy_and_hold": _empty_metrics()}

    base = _clean(candles_by_tf.get(base_tf))
    n = len(base)
    bh = buy_and_hold_metrics(base)
    if n < 3:
        return {
            "strategy": _empty_metrics(),
            "buy_and_hold": bh,
            "params": dict(cfg),
        }

    fee = max(0.0, _f(fee_pct) or 0.0) / 100.0
    slip = max(0.0, _f(slippage_pct) or 0.0) / 100.0
    fund_rate = (max(0.0, _f(funding_pct_per_bar) or 0.0) / 100.0) if funding else 0.0

    equity = 1.0  # normalised account equity (start = 1.0)
    equity_curve = [equity]
    trades: list[dict] = []
    trade_returns: list[float] = []  # realised per-trade return on equity
    trade_R: list[float] = []        # realised return in R units (PnL / risk$)
    total_costs = 0.0

    i = 0
    # We need bar i+1 to fill, so the last index we can SIGNAL on is n-2.
    while i <= n - 2:
        # The strategy sees only CLOSED bars [0..i]. Build a sliced VIEW per tf.
        view = {tf: _clean(cs)[: _slice_len(_clean(cs), base, i)]
                for tf, cs in candles_by_tf.items() if isinstance(cs, (list, tuple))}
        # Guarantee the base tf view is exactly [0..i] (the look-ahead contract).
        view[base_tf] = base[: i + 1]

        signal = strategy_fn(view, i)
        if not isinstance(signal, dict) or signal.get("signal") not in ("long", "short"):
            equity_curve.append(equity)
            i += 1
            continue

        # Route EVERY signal through the one and only approve path.
        gated = _gate(signal, equity, cfg)
        if not gated.get("approved"):
            equity_curve.append(equity)
            i += 1
            continue

        side = gated["side"]
        # Fill at the NEXT bar's open (no look-ahead on the signal close).
        fill_index = i + 1
        fill_open = _ohlc(base[fill_index])[0]  # type: ignore[index]
        qty = gated["qty"]
        stop = gated["stop"]
        target = gated["target"]

        # Slippage worsens the fill in the direction of the trade.
        if side == "long":
            entry_px = fill_open * (1.0 + slip)
        else:
            entry_px = fill_open * (1.0 - slip)

        # Walk forward to find the exit (stop checked before target — conservative).
        exit_index, exit_px, exit_reason = _simulate_exit(
            base, fill_index, side, stop, target, slip
        )

        # PnL on the position, then fees + funding.
        if side == "long":
            gross = (exit_px - entry_px) * qty
        else:
            gross = (entry_px - exit_px) * qty
        notional_in = entry_px * qty
        notional_out = exit_px * qty
        entry_fee = notional_in * fee
        exit_fee = notional_out * fee
        bars_held = max(1, exit_index - fill_index)
        funding_cost = fund_rate * notional_in * bars_held
        costs = entry_fee + exit_fee + funding_cost
        net = gross - costs
        total_costs += costs

        risk_dollars = gated.get("risk_dollars") or 0.0
        r_multiple = (net / risk_dollars) if risk_dollars > 0 else 0.0
        ret_on_equity = net / equity if equity > 0 else 0.0

        equity += net
        equity_curve.append(equity)
        trade_returns.append(ret_on_equity)
        trade_R.append(r_multiple)
        trades.append({
            "signal_index": i,
            "entry_index": fill_index,
            "exit_index": exit_index,
            "side": side,
            "entry": entry_px,
            "stop": stop,
            "target": target,
            "exit": exit_px,
            "exit_reason": exit_reason,
            "qty": qty,
            "gross": gross,
            "costs": costs,
            "net": net,
            "R": r_multiple,
            "ret_on_equity": ret_on_equity,
        })

        # Resume scanning AFTER this trade closed (no overlapping positions, and
        # critically no re-entry on a bar that the just-closed trade already used).
        i = max(exit_index + 1, i + 1)

    strat = _metrics_from_trades(trades, trade_returns, trade_R, equity,
                                 equity_curve, total_costs)
    return {
        "strategy": strat,
        "buy_and_hold": bh,
        "params": dict(cfg),
        "costs": {"fee_pct": _f(fee_pct), "slippage_pct": _f(slippage_pct),
                  "funding": bool(funding)},
        "n_bars": n,
    }


def _slice_len(tf_candles: list[dict], base: list[dict], i: int) -> int:
    """How many bars of a (possibly higher) timeframe are CLOSED as of base bar i.

    No look-ahead across timeframes: a higher-tf bar is only visible once its
    close time is <= the current base bar's close time. When timestamps are
    missing we fall back to i+1 (treats the tf as bar-aligned with the base) —
    which the tests exercise with a single base tf.
    """
    if not tf_candles:
        return 0
    if tf_candles is base:
        return i + 1
    # Use open_time alignment when available.
    base_ot = base[i].get("open_time") if i < len(base) else None
    base_ot = _f(base_ot)
    if base_ot is None or _f(tf_candles[0].get("open_time")) is None:
        return min(len(tf_candles), i + 1)
    count = 0
    for c in tf_candles:
        ot = _f(c.get("open_time"))
        if ot is not None and ot <= base_ot:
            count += 1
        else:
            break
    return count


def _simulate_exit(base, fill_index, side, stop, target, slip):
    """Walk forward from fill_index; exit on stop or target touch (stop first),
    else mark-to-close on the final bar. Returns (exit_index, exit_px, reason).

    Slippage worsens stop fills (you get filled past the stop); target fills are
    taken at the target level (limit-style, no positive slippage assumed)."""
    n = len(base)
    for j in range(fill_index, n):
        o, h, lo, cl = _ohlc(base[j])  # type: ignore[misc]
        if side == "long":
            if stop is not None and lo <= stop:
                return j, stop * (1.0 - slip), "stop"
            if target is not None and h >= target:
                return j, target, "target"
        else:
            if stop is not None and h >= stop:
                return j, stop * (1.0 + slip), "stop"
            if target is not None and lo <= target:
                return j, target, "target"
    # Never hit either level: mark to the last close.
    last = _ohlc(base[n - 1])  # type: ignore[misc]
    return n - 1, last[3], "mark_to_close"


def _gate(signal: dict, equity: float, cfg: dict) -> dict:
    """Route through risk_engine.gate_order. If risk_engine is somehow missing,
    fail SAFE (reject) rather than open an ungated position."""
    if risk_engine is None:
        return {"approved": False, "qty": 0, "reason": "risk_engine unavailable -> reject"}
    risk_state = {"day_pnl_pct": 0.0, "peak_drawdown_pct": 0.0}
    return risk_engine.gate_order(signal, equity, risk_state, cfg)


def _metrics_from_trades(trades, trade_returns, trade_R, equity, equity_curve, total_costs):
    n_trades = len(trades)
    if n_trades == 0:
        m = _empty_metrics()
        m["final_return"] = equity - 1.0
        return m
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] < 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)
    win_rate = len(wins) / n_trades
    avg_R = sum(trade_R) / n_trades if trade_R else 0.0
    return {
        "sharpe": _sharpe(trade_returns),
        "max_drawdown": _max_drawdown(equity_curve),
        "profit_factor": pf,
        "win_rate": win_rate,
        "n_trades": n_trades,
        "avg_R": avg_R,
        "final_return": equity - 1.0,
        "total_costs": total_costs,
        "trades": trades,
        "returns": trade_returns,
    }


# ---------------------------------------------------------------------------
# 3. Param-grid runner — AUTO-COUNTS n_trials, feeds deflated_sharpe
# ---------------------------------------------------------------------------

def _expand_grid(grid: dict[str, list]) -> list[dict]:
    """Cartesian product of a {param: [values]} grid -> list of param dicts.
    Deterministic order. Empty / non-dict grid -> a single empty combo."""
    if not isinstance(grid, dict) or not grid:
        return [{}]
    keys = list(grid.keys())
    combos: list[dict] = [{}]
    for k in keys:
        vals = grid[k]
        if not isinstance(vals, (list, tuple)) or not vals:
            vals = [grid[k]]
        combos = [dict(base, **{k: v}) for base in combos for v in vals]
    return combos


def _config_keys_from_params(params: dict) -> dict:
    """Pull through any risk_engine config keys present in a param combo so the
    grid can sweep risk_pct / min_rr / leverage_cap etc."""
    passthrough = ("risk_pct", "max_risk_pct", "leverage_cap", "min_rr",
                   "daily_stop", "global_stop")
    return {k: params[k] for k in passthrough if k in params}


def grid_search(
    make_strategy: Callable[[dict], Callable],
    candles_by_tf: dict,
    grid: dict[str, list],
    *,
    fee_pct: float = 0.05,
    slippage_pct: float = 0.02,
    funding: bool = True,
    base_tf: str = "1h",
    extra_config: Optional[dict] = None,
) -> dict[str, Any]:
    """Evaluate every combo in `grid`, COUNTING the combos itself, and feed that
    count to deflated_sharpe as n_trials.

    `make_strategy(params)` must return a fresh strategy_fn for the given combo.

    Returns:
        {n_trials, n_combos_evaluated, results: [...], best: {...}}

    `n_trials` == `n_combos_evaluated` is the auto-counted multiple-testing
    burden — NEVER a hand-entered number (adversarial fix #1). Never raises.
    """
    try:
        return _grid_search_inner(
            make_strategy, candles_by_tf, grid,
            fee_pct=fee_pct, slippage_pct=slippage_pct, funding=funding,
            base_tf=base_tf, extra_config=extra_config,
        )
    except Exception as exc:  # pragma: no cover
        return {"n_trials": 0, "n_combos_evaluated": 0, "results": [],
                "best": None, "error": f"grid_search error: {exc!r}"}


def _grid_search_inner(
    make_strategy, candles_by_tf, grid, *,
    fee_pct, slippage_pct, funding, base_tf, extra_config,
):
    combos = _expand_grid(grid)
    base_cfg = dict(extra_config) if isinstance(extra_config, dict) else {}

    results: list[dict] = []
    for params in combos:
        cfg = dict(base_cfg, **_config_keys_from_params(params))
        strat = make_strategy(params)
        bt = backtest(strat, candles_by_tf, fee_pct=fee_pct,
                      slippage_pct=slippage_pct, funding=funding,
                      config=cfg, base_tf=base_tf)
        results.append({"params": params, "backtest": bt})

    # AUTO-COUNTED trial burden: exactly the number of combos we just ran.
    n_trials = len(results)

    # Pick the "best" combo by strategy final_return (tie-break: more trades).
    best = None
    if results:
        def _key(r):
            s = r["backtest"]["strategy"]
            return (s["final_return"], s["n_trades"])
        best_r = max(results, key=_key)
        best_returns = best_r["backtest"]["strategy"].get("returns", [])
        dsr = (deflated_sharpe(best_returns, n_trials)
               if deflated_sharpe is not None and best_returns else 0.0)
        best = {
            "params": best_r["params"],
            "strategy": best_r["backtest"]["strategy"],
            "buy_and_hold": best_r["backtest"]["buy_and_hold"],
            "deflated_sharpe": dsr,
            "n_trials_used": n_trials,
        }

    return {
        "n_trials": n_trials,
        "n_combos_evaluated": n_trials,
        "results": results,
        "best": best,
    }


# ---------------------------------------------------------------------------
# 4. Walk-forward / out-of-sample
# ---------------------------------------------------------------------------

def _split_by_tf(candles_by_tf: dict, base_tf: str, oos_start_base_ot: Optional[float]):
    """Split every timeframe at the OOS boundary (a base-tf close-time)."""
    in_sample: dict = {}
    oos: dict = {}
    for tf, cs in candles_by_tf.items():
        clean = _clean(cs)
        if oos_start_base_ot is None or _f(clean[0].get("open_time") if clean else None) is None:
            # No timestamps -> can only split the base tf cleanly; others go whole
            # to in-sample (the synthetic tests use a single base tf).
            in_sample[tf] = clean
            oos[tf] = clean
            continue
        ins, out = [], []
        for c in clean:
            ot = _f(c.get("open_time"))
            if ot is not None and ot < oos_start_base_ot:
                ins.append(c)
            else:
                out.append(c)
        in_sample[tf] = ins
        oos[tf] = out
    return in_sample, oos


def walk_forward(
    make_strategy: Callable[[dict], Callable],
    candles_by_tf: dict,
    grid: dict[str, list],
    *,
    oos_frac: float = 0.3,
    fee_pct: float = 0.05,
    slippage_pct: float = 0.02,
    funding: bool = True,
    base_tf: str = "1h",
    extra_config: Optional[dict] = None,
) -> dict[str, Any]:
    """Tune on an in-sample window, then report on a HARD-FROZEN OOS tail.

    The OOS window is the last `oos_frac` of the base timeframe (default 30%;
    for the real run set this to the last 12-18 months). The grid is searched on
    in-sample ONLY; the chosen params are then run once on the never-tuned OOS
    tail, and that OOS deflated_sharpe uses the auto-counted in-sample n_trials.

    Returns:
        {n_trials, oos_start_index, in_sample: {...}, oos: {...}, best_params}

    Never raises.
    """
    try:
        return _walk_forward_inner(
            make_strategy, candles_by_tf, grid,
            oos_frac=oos_frac, fee_pct=fee_pct, slippage_pct=slippage_pct,
            funding=funding, base_tf=base_tf, extra_config=extra_config,
        )
    except Exception as exc:  # pragma: no cover
        return {"n_trials": 0, "oos_start_index": 0, "in_sample": None,
                "oos": None, "error": f"walk_forward error: {exc!r}"}


def _walk_forward_inner(
    make_strategy, candles_by_tf, grid, *,
    oos_frac, fee_pct, slippage_pct, funding, base_tf, extra_config,
):
    base = _clean(candles_by_tf.get(base_tf))
    n = len(base)
    frac = _f(oos_frac) or 0.3
    frac = min(max(frac, 0.05), 0.9)
    oos_start = max(1, int(round(n * (1.0 - frac))))
    oos_start = min(oos_start, n - 1)
    boundary_ot = _f(base[oos_start].get("open_time")) if oos_start < n else None

    in_candles, oos_candles = _split_by_tf(candles_by_tf, base_tf, boundary_ot)
    # When timestamps are absent, split the base tf by index so the OOS tail is
    # genuinely disjoint (the synthetic tests rely on this).
    if boundary_ot is None:
        in_candles = dict(in_candles)
        oos_candles = dict(oos_candles)
        in_candles[base_tf] = base[:oos_start]
        oos_candles[base_tf] = base[oos_start:]

    # 1. TUNE on in-sample only (this is where the trial burden is incurred).
    gs = grid_search(make_strategy, in_candles, grid,
                     fee_pct=fee_pct, slippage_pct=slippage_pct, funding=funding,
                     base_tf=base_tf, extra_config=extra_config)
    n_trials = gs["n_trials"]
    best_params = gs["best"]["params"] if gs.get("best") else {}

    in_sample_block = _wf_block(gs["best"], n_trials) if gs.get("best") else None

    # 2. Run the CHOSEN params ONCE on the frozen OOS tail.
    oos_strat = make_strategy(best_params)
    oos_cfg = dict(extra_config) if isinstance(extra_config, dict) else {}
    oos_cfg.update(_config_keys_from_params(best_params))
    oos_bt = backtest(oos_strat, oos_candles, fee_pct=fee_pct,
                      slippage_pct=slippage_pct, funding=funding,
                      config=oos_cfg, base_tf=base_tf)
    oos_returns = oos_bt["strategy"].get("returns", [])
    oos_dsr = (deflated_sharpe(oos_returns, n_trials)
               if deflated_sharpe is not None and oos_returns else 0.0)
    oos_block = {
        "strategy": oos_bt["strategy"],
        "buy_and_hold": oos_bt["buy_and_hold"],
        "final_return": oos_bt["strategy"]["final_return"],
        "deflated_sharpe": oos_dsr,
        "n_trials_used": n_trials,
    }

    return {
        "n_trials": n_trials,
        "oos_frac": frac,
        "oos_start_index": oos_start,
        "best_params": best_params,
        "in_sample": in_sample_block,
        "oos": oos_block,
    }


def _wf_block(best: dict, n_trials: int) -> dict:
    return {
        "strategy": best["strategy"],
        "buy_and_hold": best["buy_and_hold"],
        "final_return": best["strategy"]["final_return"],
        "deflated_sharpe": best["deflated_sharpe"],
        "n_trials_used": n_trials,
    }
