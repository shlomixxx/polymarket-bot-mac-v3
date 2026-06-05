import trade_coach as tc


def _row(status="WIN", side="Up", exit_type="settle", pnl=10.0, peak=30.0, trough=-10.0,
         schema=0, conflict=None):
    return {"settlement_status": status, "side": side, "exit_type": exit_type,
            "realized_pnl": pnl, "peak_unrealized_pct": peak, "trough_unrealized_pct": trough,
            "schema_version": schema, "signal_conflict": conflict}


def test_exit_discipline_fires_when_tp_beats_settle():
    rows = ([_row("WIN", exit_type="TP", pnl=14.0) for _ in range(20)] +
            [_row("LOSS", exit_type="settle", pnl=-5.0, peak=25.0) for _ in range(14)] +
            [_row("WIN", exit_type="settle", pnl=8.0, peak=25.0) for _ in range(6)])
    out = tc.compute_lessons(rows)
    keys = {l["key"] for l in out["lessons"]}
    assert "exit_discipline" in keys
    lesson = next(l for l in out["lessons"] if l["key"] == "exit_discipline")
    assert lesson["severity"] == "critical"
    assert lesson["stat"]["tp_winrate"] > lesson["stat"]["settle_winrate"]


def test_green_turned_red_fires():
    losses = [_row("LOSS", pnl=-5.0, peak=40.0) for _ in range(15)] + \
             [_row("LOSS", pnl=-5.0, peak=-2.0) for _ in range(10)]
    wins = [_row("WIN", pnl=8.0) for _ in range(10)]
    out = tc.compute_lessons(losses + wins)
    g = next((l for l in out["lessons"] if l["key"] == "green_turned_red"), None)
    assert g is not None
    assert g["stat"]["pct_losses_reached_peak"] == 60.0  # 15/25


def test_side_edge_reports_better_side():
    rows = ([_row("WIN", side="Down") for _ in range(33)] + [_row("LOSS", side="Down") for _ in range(17)] +
            [_row("WIN", side="Up") for _ in range(25)] + [_row("LOSS", side="Up") for _ in range(25)])
    out = tc.compute_lessons(rows)
    s = next((l for l in out["lessons"] if l["key"] == "side_edge"), None)
    assert s is not None and s["stat"]["better_side"] == "Down"


def test_martingale_risk_fires_on_extreme_tail():
    rows = [_row("LOSS", pnl=-10.0) for _ in range(30)] + [_row("LOSS", pnl=-5000.0)]
    out = tc.compute_lessons(rows)
    m = next((l for l in out["lessons"] if l["key"] == "martingale_risk"), None)
    assert m is not None
    assert m["stat"]["max_single_loss"] == -5000.0
    assert m["stat"]["n_losses_over_500"] == 1


def test_signals_pending_until_live_rows():
    out = tc.compute_lessons([_row(schema=0) for _ in range(50)])
    p = next((l for l in out["lessons"] if l["key"] == "signals_pending"), None)
    assert p is not None
    assert out["eras"]["schema_v1"] == 0


def test_signal_conflict_fires_with_enough_live_rows():
    rows = ([_row("LOSS", schema=1, conflict=True) for _ in range(14)] +
            [_row("WIN", schema=1, conflict=True) for _ in range(6)] +
            [_row("WIN", schema=1, conflict=False) for _ in range(16)] +
            [_row("LOSS", schema=1, conflict=False) for _ in range(4)])
    out = tc.compute_lessons(rows)
    c = next((l for l in out["lessons"] if l["key"] == "signal_conflict"), None)
    assert c is not None
    assert c["stat"]["agree_winrate"] > c["stat"]["conflict_winrate"]


def test_drawdown_denominator_excludes_rows_without_trough():
    # 50 deep-dip rows (trough -90) + 50 rows with NO trough reading.
    deep = [_row("WIN", trough=-90.0) for _ in range(50)]
    no_trough = [_row("WIN", trough=None) for _ in range(50)]
    out = tc.compute_lessons(deep + no_trough)
    d = next((l for l in out["lessons"] if l["key"] == "drawdown"), None)
    assert d is not None
    # denominator is rows-with-a-trough (50), so 50/50 = 100%, not 50/100 = 50%
    assert d["stat"]["pct_dipped_below_minus50"] == 100.0


def test_compute_lessons_never_raises_on_non_dict_rows():
    out = tc.compute_lessons([None, 5, "x", {"settlement_status": "WIN", "realized_pnl": 1.0}])
    assert out["eras"]["total"] == 1  # the non-dicts are filtered out, no crash


def test_compute_lessons_is_robust_and_sorted():
    out = tc.compute_lessons([])           # empty -> no crash
    assert out["lessons"] == [] or all("severity" in l for l in out["lessons"])
    assert out["eras"]["total"] == 0
    # severities are sorted critical->low
    rows = ([_row("WIN", exit_type="TP", pnl=14.0) for _ in range(20)] +
            [_row("LOSS", exit_type="settle", pnl=-5.0, peak=25.0) for _ in range(20)])
    sevs = [tc.SEV_ORDER[l["severity"]] for l in tc.compute_lessons(rows)["lessons"]]
    assert sevs == sorted(sevs)
