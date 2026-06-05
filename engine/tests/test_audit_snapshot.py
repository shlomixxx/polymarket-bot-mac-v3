import audit_snapshot as asnap


def test_build_decision_snapshot_shape():
    snap = asnap.build_decision_snapshot(
        mode="demo", side="Up", slug="btc-updown-5m-123", epoch=123, window_sec=300,
        decision_ts_ms=1733385600123, code_version="abc123",
        signal_result={"recommendation": "Up", "up_confidence": 0.63, "down_confidence": 0.37,
                       "weighted_score": 0.26, "confidence_pct": 63.0,
                       "sub": {"ta": {"rsi": 58.2, "ta_score": 2},
                               "clob": {"net_score": 0.2, "up_ask": 0.52, "down_ask": 0.50, "spread": 0.02},
                               "sentiment": {"funding_rate_pct": -0.01, "fear_greed_value": 41, "sentiment_score": 1},
                               "history": {"hour_up_rate": 0.57, "hour_sample_size": 120, "overall_up_rate": 0.51}}},
        policy={"order_mode": "market", "take_profit_pct": 50, "entry_price_cents_cap": 65,
                "loss_recovery_enabled": True, "loss_recovery_multiplier": 2.0, "loss_recovery_streak": 1},
        book={"ask_u": 0.52, "bid_u": 0.50, "ask_d": 0.50, "bid_d": 0.48},
        provenance={"btc_spot_source": "ws", "btc_spot_age_ms": 120, "book_source": "ws", "book_age_ms": 80},
        regime={"vol_bucket": "mid", "btc_change_pct_at_entry": 0.05,
                "seconds_remaining_at_entry": 210, "entry_minute_in_window": 1},
        execution={"avg_fill_price": 0.52, "contracts": 40.0, "gate": "signal", "reason": "auto"},
        btc_spot_at_entry=64000.0,
    )
    assert snap["schema_version"] == 1
    assert snap["side"] == "Up"
    assert snap["signal"]["recommendation"] == "Up"
    assert snap["ta"]["ta_score"] == 2
    assert snap["clob"]["down_ask"] == 0.50
    assert snap["policy"]["loss_recovery_multiplier"] == 2.0
    assert snap["provenance"]["btc_spot_source"] == "ws"
    assert snap["execution"]["contracts"] == 40.0


def test_build_marks_missing_signals():
    snap = asnap.build_decision_snapshot(
        mode="demo", side="Up", slug="s", epoch=1, window_sec=300,
        decision_ts_ms=1, code_version="x", signal_result=None,
        policy={}, book={}, provenance={}, regime={}, execution={}, btc_spot_at_entry=None)
    assert snap["schema_version"] == 1
    assert snap["provenance"]["signals_missing"] is True
    assert snap["signal"] == {}
