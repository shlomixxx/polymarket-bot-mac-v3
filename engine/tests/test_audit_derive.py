import audit_derive as ad


def test_settlement_status_win_loss_void_unknown_pending():
    assert ad.settlement_status({"type": "SETTLE_WIN", "realized_pnl": 4.0}) == "WIN"
    assert ad.settlement_status({"type": "SETTLE_LOSS", "realized_pnl": -2.0}) == "LOSS"
    assert ad.settlement_status({"type": "SELL_TP", "realized_pnl": 3.0}) == "WIN"
    assert ad.settlement_status({"type": "SELL_TP", "realized_pnl": -1.0}) == "LOSS"
    assert ad.settlement_status({"type": "SETTLE_WIN", "voided": True}) == "VOID"
    assert ad.settlement_status({"type": "SETTLE_UNKNOWN"}) == "UNKNOWN"
    assert ad.settlement_status({"type": "SETTLE_WIN", "settlement_error": "x"}) == "UNKNOWN"
    assert ad.settlement_status({"type": "BUY"}) == "PENDING"


def test_exit_efficiency_bounded_and_guards():
    assert ad.exit_efficiency(realized_pct=40.0, peak_pct=80.0) == 0.5
    # held-to-settlement win exceeding the intraday peak caps at 1.0 (not >1)
    assert ad.exit_efficiency(realized_pct=99.0, peak_pct=40.0) == 1.0
    # positive realized with no recorded intraday peak = captured everything -> 1.0
    assert ad.exit_efficiency(realized_pct=10.0, peak_pct=0.0) == 1.0
    # losses have no meaningful exit-efficiency
    assert ad.exit_efficiency(realized_pct=-100.0, peak_pct=5.0) is None
    # no upside at all -> None
    assert ad.exit_efficiency(realized_pct=0.0, peak_pct=0.0) is None


def test_cf_other_side_pnl_binary():
    out = ad.cf_other_side_pnl(
        side="Up", resolved_outcome="Down", contracts=40.0,
        opposite_ask=0.50, fee_rate=0.0,
    )
    assert out == 20.0
    assert ad.cf_other_side_pnl(side="Up", resolved_outcome=None, contracts=40.0,
                                opposite_ask=0.50, fee_rate=0.0) is None


def test_signal_was_correct_only_when_resolved():
    assert ad.signal_was_correct(side="Up", resolved_outcome="Up") is True
    assert ad.signal_was_correct(side="Up", resolved_outcome="Down") is False
    assert ad.signal_was_correct(side="Up", resolved_outcome=None) is None


def test_signals_agreement_and_conflict():
    snap = {"ta": {"ta_score": 2}, "clob": {"net_score": 0.2},
            "sentiment": {"sentiment_score": 1}, "signal": {"recommendation": "Up"}}
    agree = ad.signals_agreement(snap)
    assert 0.0 <= agree <= 1.0 and agree >= 0.66
    assert ad.signal_conflict(snap, side="Up") is False
    assert ad.signal_conflict(snap, side="Down") is True


def test_agreement_reads_real_compute_signals_score_keys():
    # compute_signals' ta/sentiment sub-dicts use "score" (not ta_score/sentiment_score).
    snap = {"ta": {"score": 2}, "clob": {"net_score": 0.3},
            "sentiment": {"score": 1}, "signal": {"recommendation": "Up"}}
    assert ad.signals_agreement(snap) >= 0.66
    assert ad.signal_conflict(snap, side="Up") is False
    assert ad.signal_conflict(snap, side="Down") is True


def test_lesson_tag_classifies():
    assert ad.lesson_tag(status="WIN", exit_eff=0.95, signal_correct=True, conflict=False) == "clean_win"
    assert ad.lesson_tag(status="WIN", exit_eff=0.3, signal_correct=True, conflict=False) == "good_entry_late_exit"
    assert ad.lesson_tag(status="LOSS", exit_eff=None, signal_correct=False, conflict=True) == "signal_conflict_loss"
    assert ad.lesson_tag(status="VOID", exit_eff=None, signal_correct=None, conflict=False) == "void_no_signal"


def test_rule_flags_against_recommendation_and_neutral():
    # entered Down while the headline recommendation was Up -> against_recommendation
    snap_against = {"side": "Down", "signal": {"recommendation": "Up"}}
    rf = ad.rule_flags(snap_against, {})
    assert rf["against_recommendation"] is True
    assert rf["entered_on_neutral"] is False

    # entered the same side the recommendation pointed to -> not against
    snap_aligned = {"side": "Up", "signal": {"recommendation": "Up"}}
    rf2 = ad.rule_flags(snap_aligned, {})
    assert rf2["against_recommendation"] is False
    assert rf2["entered_on_neutral"] is False

    # neutral recommendation -> entered_on_neutral, never "against"
    snap_neutral = {"side": "Up", "signal": {"recommendation": "neutral"}}
    rf3 = ad.rule_flags(snap_neutral, {})
    assert rf3["against_recommendation"] is False
    assert rf3["entered_on_neutral"] is True

    # missing recommendation -> both honest flags False (no signal to be against)
    rf4 = ad.rule_flags({"side": "Up", "signal": {}}, {})
    assert rf4["against_recommendation"] is False
    assert rf4["entered_on_neutral"] is False
    # the legacy component-majority flag is still present
    assert "against_signal" in rf4


def test_derive_learning_fields_end_to_end():
    snapshot = {
        "side": "Up", "execution": {"contracts": 40.0, "avg_fill_price": 0.52},
        "ta": {"ta_score": 2}, "clob": {"net_score": 0.2, "down_ask": 0.50},
        "sentiment": {"sentiment_score": 1}, "signal": {"recommendation": "Up"},
    }
    outcome = {
        "type": "SETTLE_LOSS", "realized_pnl": -20.8, "realized_pct": -100.0,
        "peak_unrealized_pct": 5.0, "trough_unrealized_pct": -100.0,
        "resolved_outcome": "Down", "settlement_won": False,
        "fee_rate": 0.0,
    }
    d = ad.derive_learning_fields(snapshot, outcome)
    assert d["settlement_status"] == "LOSS"
    assert d["signal_was_correct"] is False
    assert d["cf_other_side_pnl"] == 20.0
    assert d["lesson_tag"] in {"signal_conflict_loss", "wrong_side_loss"}
    assert "cf_exit_variants" in d and "rule_flags" in d
