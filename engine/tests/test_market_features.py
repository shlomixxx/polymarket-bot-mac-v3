"""
Unit tests for the prediction-market features helper (recording-only).

The Polymarket share asks ARE the market's implied probabilities. These features
are stamped into audit_inputs for a future learner; they must NOT change the trade.
"""
import math

import clob_imbalance as ci


def test_market_features_basic():
    # up_ask=0.55, down_ask=0.50 -> vig = 0.55+0.50-1 = 0.05
    m = ci.market_features(up_ask=0.55, down_ask=0.50, model_up_prob=0.60)
    assert math.isclose(m["up_ask"], 0.55)
    assert math.isclose(m["down_ask"], 0.50)
    assert math.isclose(m["vig"], 0.05, rel_tol=1e-9)
    # normalized: 0.55/1.05 and 0.50/1.05 (values are rounded to 6dp in the ledger)
    assert math.isclose(m["up_implied_prob"], round(0.55 / 1.05, 6), rel_tol=1e-9)
    assert math.isclose(m["down_implied_prob"], round(0.50 / 1.05, 6), rel_tol=1e-9)
    # edge = model_up_prob - up_implied_prob (computed pre-round on the prob)
    assert math.isclose(
        m["ta_vs_market_edge_up"], round(0.60 - (0.55 / 1.05), 6), rel_tol=1e-9
    )


def test_market_implied_probs_sum_to_one():
    m = ci.market_features(up_ask=0.7, down_ask=0.4, model_up_prob=0.5)
    assert math.isclose(m["up_implied_prob"] + m["down_implied_prob"], 1.0, rel_tol=1e-9)


def test_market_features_none_model_prob_treated_as_zero_edge_base():
    # model_up_prob None -> edge None (cannot compute), other fields still present
    m = ci.market_features(up_ask=0.55, down_ask=0.50, model_up_prob=None)
    assert m["vig"] is not None
    assert m["up_implied_prob"] is not None
    assert m["ta_vs_market_edge_up"] is None


def test_market_features_guards_missing_asks():
    m = ci.market_features(up_ask=None, down_ask=0.5, model_up_prob=0.5)
    assert m["up_ask"] is None
    assert m["vig"] is None
    assert m["up_implied_prob"] is None
    assert m["down_implied_prob"] is None
    assert m["ta_vs_market_edge_up"] is None


def test_market_features_zero_denominator_guard():
    # both asks zero -> denom zero -> implied probs None, never raise
    m = ci.market_features(up_ask=0.0, down_ask=0.0, model_up_prob=0.5)
    assert m["up_implied_prob"] is None
    assert m["down_implied_prob"] is None
    assert m["ta_vs_market_edge_up"] is None
    # vig is still computable: 0 + 0 - 1 = -1
    assert math.isclose(m["vig"], -1.0, rel_tol=1e-9)
