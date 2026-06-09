"""
Tests for engine/backtester.py — the honesty gate.

PURE parts only (no network): bar-by-bar replay on a synthetic series, the
no-look-ahead invariant, fee/slippage cost monotonicity, buy-and-hold maths,
and the auto-counted n_trials wired from the param-grid runner into
deflated_sharpe.

Bare imports (engine/ on path via conftest.py).
Run: python3 -m pytest engine/tests/test_backtester.py -q
"""
from __future__ import annotations

import backtester as B
from backtester import (
    backtest,
    buy_and_hold_metrics,
    fetch_history,
    grid_search,
    walk_forward,
)


# ---------------------------------------------------------------------------
# Candle builder + synthetic series
# ---------------------------------------------------------------------------

def c(o, h, l, cl, *, vol=1.0, t=None):
    d = {"open": o, "high": h, "low": l, "close": cl, "volume": vol}
    if t is not None:
        d["open_time"] = t
    return d


def _uptrend(n=60, start=100.0, step=2.0, t0=0, dt_ms=3_600_000):
    """A clean rising series; each bar's range straddles the close so a stop /
    target placed inside the bar can be hit deterministically."""
    out = []
    px = start
    for i in range(n):
        o = px
        cl = px + step
        out.append(c(o, cl + step, o - step, cl, t=t0 + i * dt_ms))
        px = cl
    return out


# A trivial deterministic strategy used to drive the replay engine without
# depending on the full multi-timeframe btc_strategy. It goes long on the FIRST
# closed bar it sees, with a stop below and a 2:1 target above — then flat.
class _OneShotLong:
    """Callable strategy_fn. Fires a single long on the bar at `fire_index`
    (counting only CLOSED bars handed to it), and records the largest index it
    was ever allowed to see — used to prove no look-ahead."""

    def __init__(self, fire_index=5, risk=4.0, rr=2.0):
        self.fire_index = fire_index
        self.risk = risk
        self.rr = rr
        self.max_index_seen = -1
        self.fired = False

    def __call__(self, candles_by_tf, i):
        # The engine must only ever hand us bars up to and including index i.
        self.max_index_seen = max(self.max_index_seen, i)
        bars = candles_by_tf["1h"][: i + 1]
        # Look-ahead tripwire: the slice we are given must never exceed i+1.
        assert len(bars) == i + 1
        if self.fired or i != self.fire_index:
            return {"signal": "flat", "entry": None, "stop": None, "target": None}
        self.fired = True
        entry = bars[-1]["close"]
        stop = entry - self.risk
        target = entry + self.rr * self.risk
        return {"signal": "long", "entry": entry, "stop": stop, "target": target}


# ---------------------------------------------------------------------------
# 1. Replay sanity + NO look-ahead
# ---------------------------------------------------------------------------

def test_replay_runs_and_reports_core_metrics():
    candles = {"1h": _uptrend(60)}
    strat = _OneShotLong(fire_index=5)
    res = backtest(strat, candles, fee_pct=0.05, slippage_pct=0.02, funding=False,
                   config={"risk_pct": 1.0})
    s = res["strategy"]
    for key in ("sharpe", "max_drawdown", "profit_factor", "win_rate",
                "n_trades", "avg_R", "final_return"):
        assert key in s, key
    assert "buy_and_hold" in res
    assert s["n_trades"] >= 1


def test_no_look_ahead_strategy_never_sees_a_future_bar():
    candles = {"1h": _uptrend(40)}
    strat = _OneShotLong(fire_index=5)
    res = backtest(strat, candles, funding=False)
    n = len(candles["1h"])
    # The strategy must never have been shown an index beyond the last CLOSED bar.
    assert strat.max_index_seen <= n - 1
    # And the trade it took must have been filled at the NEXT bar's open, not the
    # signal bar's close (the engine fills on the bar after the signal closes).
    trades = res["strategy"]["trades"]
    assert trades, "expected at least one trade"
    t0 = trades[0]
    assert t0["entry_index"] > t0["signal_index"], (
        "fill must happen strictly after the signal bar closes (no look-ahead)"
    )


def test_trade_exit_uses_only_bars_at_or_after_entry():
    """The exit bar index must be >= the entry bar index — a trade can never be
    closed by a bar that came before it was opened."""
    candles = {"1h": _uptrend(40)}
    res = backtest(_OneShotLong(fire_index=3), candles, funding=False)
    for tr in res["strategy"]["trades"]:
        assert tr["exit_index"] >= tr["entry_index"]


# ---------------------------------------------------------------------------
# 2. Fees + slippage strictly reduce return
# ---------------------------------------------------------------------------

def test_fees_and_slippage_reduce_return():
    candles = {"1h": _uptrend(60)}
    free = backtest(_OneShotLong(fire_index=5), candles,
                    fee_pct=0.0, slippage_pct=0.0, funding=False)
    costly = backtest(_OneShotLong(fire_index=5), candles,
                      fee_pct=0.10, slippage_pct=0.05, funding=False)
    assert free["strategy"]["n_trades"] == costly["strategy"]["n_trades"] >= 1
    # Same trades, but costs eat into the realised return.
    assert costly["strategy"]["final_return"] < free["strategy"]["final_return"]
    assert costly["strategy"]["total_costs"] > free["strategy"]["total_costs"] == 0.0


def test_funding_is_an_additional_cost():
    candles = {"1h": _uptrend(60)}
    no_funding = backtest(_OneShotLong(fire_index=5), candles,
                          fee_pct=0.0, slippage_pct=0.0, funding=False)
    with_funding = backtest(_OneShotLong(fire_index=5), candles,
                            fee_pct=0.0, slippage_pct=0.0, funding=True)
    assert with_funding["strategy"]["total_costs"] >= no_funding["strategy"]["total_costs"]


# ---------------------------------------------------------------------------
# 3. Buy-and-hold computed correctly over the identical window
# ---------------------------------------------------------------------------

def test_buy_and_hold_return_matches_first_to_last_close():
    candles = _uptrend(10, start=100.0, step=10.0)  # 100 -> 200 close, then +10/bar
    bh = buy_and_hold_metrics(candles)
    first_close = candles[0]["close"]
    last_close = candles[-1]["close"]
    expected = last_close / first_close - 1.0
    assert abs(bh["final_return"] - expected) < 1e-9
    assert bh["max_drawdown"] <= 0.0  # drawdown is reported as <= 0


def test_buy_and_hold_drawdown_on_a_dip():
    # up to 120, down to 80 (a -33% drawdown from the peak), back up to 130
    closes = [100, 110, 120, 100, 80, 100, 130]
    candles = [c(p, p + 1, p - 1, p) for p in closes]
    bh = buy_and_hold_metrics(candles)
    # peak 120 -> trough 80 == -33.33%
    assert abs(bh["max_drawdown"] - (80.0 / 120.0 - 1.0)) < 1e-9
    assert abs(bh["final_return"] - (130.0 / 100.0 - 1.0)) < 1e-9


def test_backtest_reports_buy_and_hold_over_same_window():
    candles = {"1h": _uptrend(30)}
    res = backtest(_OneShotLong(fire_index=5), candles, funding=False)
    bh = buy_and_hold_metrics(candles["1h"])
    assert abs(res["buy_and_hold"]["final_return"] - bh["final_return"]) < 1e-9


# ---------------------------------------------------------------------------
# 4. grid_search auto-counts n_trials, feeds it to deflated_sharpe
# ---------------------------------------------------------------------------

def test_grid_search_counts_every_combo_as_n_trials():
    candles = {"1h": _uptrend(80)}

    def make_strategy(params):
        return _OneShotLong(fire_index=params["fire_index"])

    grid = {"fire_index": [5, 10, 15], "risk_pct": [0.5, 1.0]}  # 3 * 2 = 6 combos
    gr = grid_search(make_strategy, candles, grid, funding=False)
    assert gr["n_trials"] == 6
    assert len(gr["results"]) == 6
    # n_trials is AUTO-COUNTED, never a passed-in number.
    assert gr["n_trials"] == gr["n_combos_evaluated"]


def test_deflated_sharpe_uses_auto_counted_n_trials():
    candles = {"1h": _uptrend(120)}

    def make_strategy(params):
        return _OneShotLong(fire_index=params["fire_index"])

    grid = {"fire_index": [5, 10, 20, 30]}  # 4 combos
    gr = grid_search(make_strategy, candles, grid, funding=False)
    assert gr["n_trials"] == 4
    # The DSR on the best combo must have been computed with n_trials == 4,
    # not 1 and not a hand-entered constant.
    best = gr["best"]
    assert best["n_trials_used"] == 4
    assert 0.0 <= best["deflated_sharpe"] <= 1.0


def test_grid_search_n_trials_changes_with_grid_size():
    candles = {"1h": _uptrend(80)}

    def make_strategy(params):
        return _OneShotLong(fire_index=params["fire_index"])

    small = grid_search(make_strategy, candles, {"fire_index": [5, 10]}, funding=False)
    big = grid_search(make_strategy, candles,
                      {"fire_index": [5, 10, 15, 20, 25]}, funding=False)
    assert small["n_trials"] == 2
    assert big["n_trials"] == 5
    assert big["n_trials"] > small["n_trials"]


# ---------------------------------------------------------------------------
# 5. Walk-forward / OOS split
# ---------------------------------------------------------------------------

def test_walk_forward_splits_in_sample_and_oos_without_overlap():
    candles = {"1h": _uptrend(100)}

    def make_strategy(params):
        return _OneShotLong(fire_index=params["fire_index"])

    grid = {"fire_index": [5, 10, 15]}
    wf = walk_forward(make_strategy, candles, grid, oos_frac=0.3, funding=False)
    assert "in_sample" in wf and "oos" in wf
    # The OOS window must be the frozen TAIL, disjoint from in-sample.
    assert wf["oos_start_index"] > 0
    assert wf["oos_start_index"] < len(candles["1h"])
    # n_trials fed to the OOS deflated_sharpe equals combos tried in-sample.
    assert wf["n_trials"] == len(grid["fire_index"])
    assert wf["oos"]["n_trials_used"] == wf["n_trials"]


def test_walk_forward_oos_is_reported_separately_from_in_sample():
    candles = {"1h": _uptrend(120)}

    def make_strategy(params):
        return _OneShotLong(fire_index=params["fire_index"])

    wf = walk_forward(make_strategy, candles, {"fire_index": [5, 10]},
                      oos_frac=0.25, funding=False)
    # Both sides carry the standard metric block + a buy-and-hold comparison.
    for side in ("in_sample", "oos"):
        assert "strategy" in wf[side]
        assert "buy_and_hold" in wf[side]
        assert "final_return" in wf[side]["strategy"]


# ---------------------------------------------------------------------------
# 6. fetch_history is offline-safe and flags an unreachable-Binance run
# ---------------------------------------------------------------------------

def test_fetch_history_offline_flags_must_rerun(tmp_path):
    # Point the cache dir at an empty tmp dir and force the network closed.
    res = fetch_history(
        interval="1d", limit=50,
        cache_dir=str(tmp_path),
        _force_offline=True,
    )
    assert res["candles"], "must fall back to a synthetic series, not crash"
    assert res["source"] in ("synthetic", "cache")
    if res["source"] == "synthetic":
        assert res["needs_real_history_run"] is True
        assert res["binance_reachable"] is False


def test_fetch_history_reads_cache_when_present(tmp_path):
    import json
    interval = "1d"
    candles = _uptrend(30)
    cache_file = tmp_path / f"btc_{interval}.json"
    cache_file.write_text(json.dumps(candles))
    res = fetch_history(interval=interval, limit=30,
                        cache_dir=str(tmp_path), _force_offline=True)
    assert res["source"] == "cache"
    assert res["binance_reachable"] is False
    assert len(res["candles"]) == len(candles)
    assert res["needs_real_history_run"] is True  # cache is fine, but it's not a live fetch


# ---------------------------------------------------------------------------
# 7. No martingale surface (defense in depth, same as strategy/risk modules)
# ---------------------------------------------------------------------------

def test_no_martingale_surface_in_module():
    forbidden = ("martingale", "double_down", "size_up", "add_to_loser",
                 "average_down", "recovery_multiplier")
    public = [name for name in dir(B) if not name.startswith("__")]
    for name in public:
        low = name.lower()
        assert all(f not in low for f in forbidden), f"suspicious symbol: {name}"


def test_backtest_never_raises_on_garbage_input():
    for bad in (None, {}, {"1h": None}, {"1h": []}, {"1h": "x"}, 42):
        res = backtest(_OneShotLong(), bad if isinstance(bad, dict) else {"1h": bad},
                       funding=False)
        assert "strategy" in res
        assert res["strategy"]["n_trades"] == 0
