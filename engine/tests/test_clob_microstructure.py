"""
Unit tests for the recording-only microstructure helpers in clob_imbalance.

DATA-ONLY: these features are stamped into the audit ledger for a future learner.
They must NOT change net_score / the trading signal — see
test_clob_microstructure_does_not_change_net_score below.
"""
import math

import clob_imbalance as ci


def _book(bids, asks):
    return {
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


# ── microprice (cross-weighted size-weighted fair value) ──────────────────────
def test_microprice_cross_weighting():
    # best_bid=0.40 (size 10), best_ask=0.60 (size 30)
    # microprice = (best_bid*ask_size + best_ask*bid_size)/(bid_size+ask_size)
    #            = (0.40*30 + 0.60*10)/(10+30) = (12 + 6)/40 = 0.45
    b = _book([(0.40, 10)], [(0.60, 30)])
    micro = ci.compute_microprice(b)
    assert micro is not None
    assert math.isclose(micro, 0.45, rel_tol=1e-9)


def test_microprice_equal_sizes_is_mid():
    # equal sizes -> microprice collapses to the simple mid
    b = _book([(0.40, 5)], [(0.60, 5)])
    assert math.isclose(ci.compute_microprice(b), 0.50, rel_tol=1e-9)


def test_microprice_empty_or_one_sided_is_none():
    assert ci.compute_microprice(None) is None
    assert ci.compute_microprice({"bids": [], "asks": []}) is None
    assert ci.compute_microprice(_book([(0.4, 10)], [])) is None
    assert ci.compute_microprice(_book([], [(0.6, 10)])) is None


def test_microprice_zero_total_size_is_none():
    assert ci.compute_microprice(_book([(0.4, 0)], [(0.6, 0)])) is None


# ── L1 imbalance at the top level ─────────────────────────────────────────────
def test_l1_imbalance_top_level():
    # (bid_size - ask_size)/(bid_size + ask_size) = (30-10)/(30+10) = 0.5
    b = _book([(0.40, 30)], [(0.60, 10)])
    assert math.isclose(ci.compute_l1_imbalance(b), 0.5, rel_tol=1e-9)


def test_l1_imbalance_bounds():
    assert math.isclose(ci.compute_l1_imbalance(_book([(0.4, 10)], [(0.6, 0.0)])), 1.0)
    assert math.isclose(ci.compute_l1_imbalance(_book([(0.4, 0.0)], [(0.6, 10)])), -1.0)


def test_l1_imbalance_guards():
    assert ci.compute_l1_imbalance(None) is None
    assert ci.compute_l1_imbalance(_book([], [(0.6, 10)])) is None
    assert ci.compute_l1_imbalance(_book([(0.4, 10)], [])) is None
    assert ci.compute_l1_imbalance(_book([(0.4, 0)], [(0.6, 0)])) is None


# ── spread / spread_pct ───────────────────────────────────────────────────────
def test_spread_and_pct():
    b = _book([(0.40, 10)], [(0.60, 10)])
    spread, spread_pct = ci.compute_spread(b)
    assert math.isclose(spread, 0.20, rel_tol=1e-9)
    # mid = 0.50 -> 0.20/0.50*100 = 40.0
    assert math.isclose(spread_pct, 40.0, rel_tol=1e-9)


def test_spread_guards():
    assert ci.compute_spread(None) == (None, None)
    assert ci.compute_spread(_book([], [(0.6, 10)])) == (None, None)
    assert ci.compute_spread(_book([(0.4, 10)], [])) == (None, None)
    # mid == 0 -> spread_pct None but spread still computable
    sp, sp_pct = ci.compute_spread(_book([(0.0, 10)], [(0.0, 10)]))
    assert sp == 0.0 and sp_pct is None


# ── depth_ratio / book slope over top ~5 levels ───────────────────────────────
def test_depth_ratio_top5():
    bids = [(0.40, 10), (0.39, 10), (0.38, 10), (0.37, 10), (0.36, 10)]  # sum 50
    asks = [(0.60, 5), (0.61, 5), (0.62, 5), (0.63, 5), (0.64, 5)]       # sum 25
    b = _book(bids, asks)
    # (50-25)/(50+25) = 25/75 = 0.3333...
    assert math.isclose(ci.compute_depth_ratio(b, levels=5), 1.0 / 3.0, rel_tol=1e-9)


def test_depth_ratio_caps_at_levels():
    # 7 bid levels, levels=5 -> only first 5 counted
    bids = [(0.40, 10)] * 7  # sum of first 5 = 50
    asks = [(0.60, 10)] * 7  # sum of first 5 = 50
    b = _book(bids, asks)
    assert math.isclose(ci.compute_depth_ratio(b, levels=5), 0.0, abs_tol=1e-9)


def test_depth_ratio_guards():
    assert ci.compute_depth_ratio(None) is None
    assert ci.compute_depth_ratio({"bids": [], "asks": []}) is None
    assert ci.compute_depth_ratio(_book([(0.4, 0)], [(0.6, 0)])) is None


# ── per-side helper bundling all of the above ─────────────────────────────────
def test_microstructure_for_book_bundle():
    b = _book([(0.40, 10)], [(0.60, 30)])
    ms = ci.microstructure_for_book(b)
    assert ms["microprice"] is not None
    assert ms["l1_imbalance"] is not None
    assert ms["spread"] is not None
    assert ms["spread_pct"] is not None
    assert ms["depth_ratio"] is not None


def test_microstructure_for_book_none_safe():
    ms = ci.microstructure_for_book(None)
    assert ms == {
        "microprice": None,
        "l1_imbalance": None,
        "spread": None,
        "spread_pct": None,
        "depth_ratio": None,
    }


# ── RECORDING-ONLY guard: net_score / signal unchanged by the new fields ──────
def test_clob_microstructure_does_not_change_net_score():
    up = _book([(0.40, 30), (0.39, 10)], [(0.60, 10), (0.61, 10)])
    down = _book([(0.45, 5)], [(0.55, 20)])
    out = ci.analyze_clob_imbalance(up, down)
    # baseline net_score computed only from bid/ask DEPTH (unchanged formula)
    up_d = ci.compute_book_depth(up)
    down_d = ci.compute_book_depth(down)
    expected_net = round(
        ci.compute_imbalance_score(up_d["bid_depth"], up_d["ask_depth"])
        - ci.compute_imbalance_score(down_d["bid_depth"], down_d["ask_depth"]),
        4,
    )
    assert out["net_score"] == expected_net
    assert out["signal"] in {"up", "down", "neutral"}
    # the new microstructure fields are present and additive (recording-only)
    assert "microprice" in out["up"] and "microprice" in out["down"]
    assert "l1_imbalance" in out["up"]
    assert "depth_ratio" in out["up"]
