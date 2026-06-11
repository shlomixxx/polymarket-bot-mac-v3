"""Unit tests for binance_equity — the persistent equity tracker that feeds the
account-level loss caps (daily -3% flatten, global -10% halt).

Two layers:
  * PURE math (compute_state): first-call init, gains, drawdowns, the UTC-day
    reset (re-bases day_start but keeps the peak), and garbage -> safe 0/0.
  * Persistence (update_and_compute via tmp_path): a JSON round-trip that persists
    + reloads, and the absolute fail-safe (never raises -> inert 0/0).

The math NEVER touches files; the file layer NEVER raises. 0/0 means "no cap
tripped", so a glitch can't wrongly flatten/halt real money.
"""
from __future__ import annotations

import json

import binance_equity


# ---------------------------------------------------------------------------
# Pure math — compute_state(current_equity, prior, *, utc_date)
# ---------------------------------------------------------------------------

def test_first_call_initializes_zero_zero():
    out = binance_equity.compute_state(1000.0, None, utc_date="2026-06-12")
    assert out["day_pnl_pct"] == 0.0
    assert out["peak_drawdown_pct"] == 0.0
    # The anchor + peak seed from the current equity on the first call.
    assert out["day_start_equity"] == 1000.0
    assert out["peak_equity"] == 1000.0
    assert out["day_start_date"] == "2026-06-12"


def test_gain_raises_day_pnl_pct_and_peak():
    prior = binance_equity.compute_state(1000.0, None, utc_date="2026-06-12")
    out = binance_equity.compute_state(1050.0, prior, utc_date="2026-06-12")
    assert out["day_pnl_pct"] == 5.0           # +50 / 1000
    assert out["peak_drawdown_pct"] == 0.0     # new high -> no drawdown
    assert out["peak_equity"] == 1050.0
    assert out["day_start_equity"] == 1000.0   # same day -> anchor unchanged


def test_drop_shows_negative_drawdown_and_negative_day_pnl():
    # Climb to a peak of 1100, then fall to 990 the SAME day.
    s = binance_equity.compute_state(1000.0, None, utc_date="2026-06-12")
    s = binance_equity.compute_state(1100.0, s, utc_date="2026-06-12")
    out = binance_equity.compute_state(990.0, s, utc_date="2026-06-12")
    assert out["peak_equity"] == 1100.0                      # peak persists
    assert out["day_pnl_pct"] == -1.0                        # -10 / 1000
    assert abs(out["peak_drawdown_pct"] - (-10.0)) < 1e-9    # (990-1100)/1100*100
    assert out["peak_drawdown_pct"] < 0


def test_new_utc_day_rebases_day_start_but_peak_persists():
    # Day 1: peak rises to 1200, day starts at 1000.
    s = binance_equity.compute_state(1000.0, None, utc_date="2026-06-12")
    s = binance_equity.compute_state(1200.0, s, utc_date="2026-06-12")
    assert s["day_start_equity"] == 1000.0 and s["peak_equity"] == 1200.0

    # Day 2 opens at 1150: day_start RE-ANCHORS to 1150 (so day_pnl re-bases to 0),
    # but the all-time peak (1200) PERSISTS -> drawdown is still measured from it.
    out = binance_equity.compute_state(1150.0, s, utc_date="2026-06-13")
    assert out["day_start_equity"] == 1150.0     # re-anchored to a new day
    assert out["day_pnl_pct"] == 0.0             # equity == new day_start
    assert out["peak_equity"] == 1200.0          # peak survives the day boundary
    assert abs(out["peak_drawdown_pct"] - (-(50.0 / 1200.0) * 100.0)) < 1e-9


def test_divide_by_zero_and_garbage_are_safe():
    # current_equity is garbage -> inert 0/0.
    bad = binance_equity.compute_state("not-a-number", None, utc_date="2026-06-12")
    assert bad["day_pnl_pct"] == 0.0 and bad["peak_drawdown_pct"] == 0.0

    # prior anchors are zero/garbage -> re-seed from current equity, no div-by-zero.
    prior = {"day_start_equity": 0.0, "peak_equity": "x", "day_start_date": "2026-06-12"}
    out = binance_equity.compute_state(500.0, prior, utc_date="2026-06-12")
    assert out["day_pnl_pct"] == 0.0
    assert out["peak_drawdown_pct"] == 0.0
    assert out["day_start_equity"] == 500.0
    assert out["peak_equity"] == 500.0


# ---------------------------------------------------------------------------
# Persistence — update_and_compute(... state_path=tmp) round-trips + fail-safe
# ---------------------------------------------------------------------------

def test_file_round_trip_persists_and_reloads(tmp_path):
    path = tmp_path / "binance_equity_state.json"

    # First update: initialise at 1000 on 2026-06-12.
    r1 = binance_equity.update_and_compute(
        1000.0, now_ts=1_700_000_000.0, utc_date="2026-06-12", state_path=path
    )
    assert r1["day_pnl_pct"] == 0.0 and r1["peak_drawdown_pct"] == 0.0
    assert path.exists()

    on_disk = json.loads(path.read_text())
    assert on_disk["day_start_equity"] == 1000.0
    assert on_disk["peak_equity"] == 1000.0
    assert on_disk["day_start_date"] == "2026-06-12"
    assert "updated_ts" in on_disk  # stamped for observability

    # Second update reloads prior state from disk: +10% same day.
    r2 = binance_equity.update_and_compute(
        1100.0, now_ts=1_700_000_010.0, utc_date="2026-06-12", state_path=path
    )
    assert abs(r2["day_pnl_pct"] - 10.0) < 1e-9
    assert r2["peak_drawdown_pct"] == 0.0
    assert r2["peak_equity"] == 1100.0

    # Third update, NEXT day, a drop: day re-bases, peak (1100) persists.
    r3 = binance_equity.update_and_compute(
        1045.0, now_ts=1_700_086_400.0, utc_date="2026-06-13", state_path=path
    )
    assert r3["day_start_equity"] == 1045.0          # re-anchored
    assert r3["day_pnl_pct"] == 0.0
    assert r3["peak_equity"] == 1100.0               # peak survived
    assert abs(r3["peak_drawdown_pct"] - (-5.0)) < 1e-9  # (1045-1100)/1100*100


def test_corrupt_state_file_is_treated_as_first_call(tmp_path):
    path = tmp_path / "binance_equity_state.json"
    path.write_text("{ this is not valid json ")
    out = binance_equity.update_and_compute(
        2000.0, now_ts=1_700_000_000.0, utc_date="2026-06-12", state_path=path
    )
    # Corrupt prior -> re-init, inert 0/0, and the file is overwritten cleanly.
    assert out["day_pnl_pct"] == 0.0 and out["peak_drawdown_pct"] == 0.0
    assert out["day_start_equity"] == 2000.0
    assert json.loads(path.read_text())["peak_equity"] == 2000.0


def test_update_and_compute_never_raises_on_bad_inputs(tmp_path):
    # Garbage equity + a path that can't be written (a directory) -> inert 0/0,
    # no exception escapes.
    out = binance_equity.update_and_compute(
        float("nan"), now_ts=0.0, utc_date="2026-06-12", state_path=tmp_path
    )
    assert out["day_pnl_pct"] == 0.0 and out["peak_drawdown_pct"] == 0.0
