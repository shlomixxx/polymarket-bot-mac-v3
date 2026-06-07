import importlib
import time


def _fresh_tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import audit_tracker
    importlib.reload(audit_tracker)  # rebind _DB_PATH to the temp DATA_ROOT
    return audit_tracker


def _snapshot(side="Up"):
    return {
        "schema_version": 1, "code_version": "abc", "decision_ts": 1733385600123,
        "mode": "demo", "side": side, "slug": "s", "epoch": 1, "window_sec": 300,
        "signal": {"recommendation": side, "weighted_score": 0.2, "confidence_pct": 60.0},
        "ta": {"ta_score": 2}, "clob": {"net_score": 0.2, "up_ask": 0.52, "down_ask": 0.50},
        "sentiment": {"sentiment_score": 1}, "history": {},
        "regime": {"vol_bucket": "mid", "seconds_remaining_at_entry": 210},
        "policy": {"loss_recovery_enabled": True, "loss_recovery_multiplier": 2.0},
        "provenance": {"btc_spot_source": "ws"},
        "execution": {"avg_fill_price": 0.52, "contracts": 40.0, "btc_spot_at_entry": 64000.0},
    }


def test_export_rows_light_skips_json_blobs_but_keeps_columns(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    at.open_row("s", _snapshot("Up"))
    at.finalize_row("s", {"type": "SETTLE_WIN", "realized_pnl": 5.0, "realized_pct": 30.0,
                          "peak_unrealized_pct": 40.0, "resolved_outcome": "Up",
                          "settled_ts": 2, "exit_type": "settle"})
    light = at.export_rows(light=True)[0]
    full = at.export_rows(light=False)[0]
    # light path skips the parsed JSON blobs ...
    assert "context" not in light and "pnl_path" not in light and "cf_exit_variants" not in light
    # ... but keeps every promoted column the coach needs, with bool coercion
    for col in ("settlement_status", "side", "exit_type", "realized_pnl",
                "peak_unrealized_pct", "schema_version", "signal_conflict"):
        assert col in light
    assert light["settlement_status"] == "WIN" and light["side"] == "Up"
    # full path still parses the blobs (unchanged behavior)
    assert "context" in full and isinstance(full["pnl_path"], list)

    # the coach produces identical lessons whether fed light or full rows
    import trade_coach
    assert trade_coach.compute_lessons(at.export_rows(light=True)) == \
           trade_coach.compute_lessons(at.export_rows(light=False))


def test_open_then_finalize_row(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    assert at.open_row("sess1", _snapshot()) is True
    at.open_row("sess1", _snapshot())
    rows = at.list_audits()
    assert len(rows) == 1
    assert rows[0]["settlement_status"] == "PENDING"

    ok = at.finalize_row("sess1", {
        "type": "SETTLE_LOSS", "realized_pnl": -20.8, "realized_pct": -100.0,
        "peak_unrealized_pct": 5.0, "trough_unrealized_pct": -100.0,
        "resolved_outcome": "Down", "settled_ts": 1733385900123,
        "settlement_btc_start": 64000.0, "settlement_btc_end": 63900.0,
        "hold_duration_sec": 300.0, "fees": 0.1, "exit_type": "settle",
    })
    assert ok is True
    row = at.get_audit("sess1")
    assert row["settlement_status"] == "LOSS"
    assert row["settled_ts"] > row["decision_ts"]
    assert row["signal_was_correct"] is False
    assert row["cf_other_side_pnl"] == 20.0
    assert row["lesson_tag"] in {"signal_conflict_loss", "wrong_side_loss", "right_side_loss"}


def test_finalize_does_not_mutate_decision_fields(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    at.open_row("s", _snapshot())
    before = at.get_audit("s")
    at.finalize_row("s", {"type": "SETTLE_WIN", "realized_pnl": 18.0, "realized_pct": 90.0,
                          "peak_unrealized_pct": 95.0, "resolved_outcome": "Up",
                          "settled_ts": 1733385900123, "exit_type": "settle"})
    after = at.get_audit("s")
    assert after["decision_ts"] == before["decision_ts"]
    assert after["recommendation"] == before["recommendation"]
    assert after["loss_recovery_multiplier"] == before["loss_recovery_multiplier"]


def test_counts_winrate_excludes_non_label(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    for i, (typ, rp, res) in enumerate([
        ("SETTLE_WIN", 10.0, "Up"), ("SETTLE_LOSS", -5.0, "Down"),
        ("SETTLE_UNKNOWN", None, None),
    ]):
        sid = f"s{i}"
        at.open_row(sid, _snapshot())
        at.finalize_row(sid, {"type": typ, "realized_pnl": rp, "resolved_outcome": res,
                              "settled_ts": 1733385900123 + i, "exit_type": "settle"})
    c = at.audit_counts()
    assert c["by_status"]["WIN"] == 1
    assert c["by_status"]["LOSS"] == 1
    assert c["by_status"]["UNKNOWN"] == 1
    assert c["win_rate_pct"] == 50.0


def test_export_excludes_non_label_rows(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    at.open_row("w", _snapshot()); at.finalize_row("w", {"type": "SETTLE_WIN", "realized_pnl": 1.0,
        "resolved_outcome": "Up", "settled_ts": 2, "exit_type": "settle"})
    at.open_row("u", _snapshot()); at.finalize_row("u", {"type": "SETTLE_UNKNOWN",
        "settled_ts": 2, "exit_type": "settle"})
    labeled = at.export_rows(labels_only=True)
    assert {r["session_id"] for r in labeled} == {"w"}


def test_never_raises_on_garbage(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    assert at.open_row("x", {"bad": object()}) in (True, False)
    assert at.finalize_row("missing", {"type": "SETTLE_WIN"}) in (True, False)
    assert isinstance(at.list_audits(), list)


def test_backfill_from_trades_is_idempotent(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    trades = [
        {"type": "BUY", "session_id": "h1", "side": "Up", "ts": 1000.0, "contracts": 40,
         "price": 0.5, "token_id": "t", "window_sec": 300, "epoch": 1, "slug": "s"},
        {"type": "SETTLE_WIN", "session_id": "h1", "side": "Up", "ts": 1300.0,
         "realized_pnl": 18.0, "resolved_outcome": "Up", "peak_unrealized_pct": 95.0,
         "settlement_btc_start": 64000.0, "settlement_btc_end": 64100.0},
    ]
    n1 = at.backfill_from_trades(trades)
    n2 = at.backfill_from_trades(trades)
    assert n1 == 1 and n2 == 0
    row = at.get_audit("h1")
    assert row["schema_version"] == 0
    assert row["settlement_status"] == "WIN"
    assert row["signal"] == {}
