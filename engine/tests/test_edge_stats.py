import math

import edge_stats as es


def test_norm_cdf_known():
    assert abs(es.norm_cdf(0.0) - 0.5) < 1e-9
    assert abs(es.norm_cdf(1.96) - 0.975) < 1e-3


def test_norm_ppf_inverse():
    assert abs(es.norm_ppf(0.975) - 1.96) < 1e-3


def test_wilson_bounds_basic():
    lo, hi = es.wilson_bounds(50, 100)
    assert lo < 0.5 < hi and 0.39 < lo < 0.41 and 0.59 < hi < 0.61


def test_wilson_degenerate():
    assert es.wilson_bounds(0, 0) == (0.0, 0.0)   # never raises


def test_two_proportion_one_sided():
    # slice 64/100 vs complement 47/100, slice>complement
    p = es.two_proportion_p(64, 100, 47, 100, alternative="greater")
    assert 0.0 < p < 0.02


def test_bh_fdr_rejects_expected():
    pvals = [0.001, 0.009, 0.02, 0.5, 0.8]
    mask = es.bh_fdr(pvals, q=0.10)
    assert mask[0] is True and mask[-1] is False


def test_bh_fdr_all_null():
    assert es.bh_fdr([0.6, 0.7, 0.9], q=0.10) == [False, False, False]


def test_bh_fdr_empty():
    assert es.bh_fdr([], q=0.10) == []


def test_day_block_bootstrap_deterministic():
    vals = [1.0, -0.5, 0.3, 0.2, -0.1, 0.4]
    days = ["a", "a", "b", "b", "c", "c"]
    out1 = es.day_block_bootstrap(vals, days, lambda xs: sum(xs) / len(xs), iters=500, seed=7)
    out2 = es.day_block_bootstrap(vals, days, lambda xs: sum(xs) / len(xs), iters=500, seed=7)
    assert out1 == out2 and len(out1) == 500


def test_tertiles_monotone():
    q33, q66 = es.tertiles(list(range(100)))
    assert q33 < q66


def test_tertiles_degenerate():
    # empty / all-same must never raise and must stay ordered
    assert es.tertiles([]) == (0.0, 0.0)
    q33, q66 = es.tertiles([5.0, 5.0, 5.0, 5.0])
    assert q33 <= q66


def test_deflated_sharpe_in_unit_interval():
    rng = __import__("random").Random(11)
    # a genuinely positive-drift return stream over many trials
    good = [0.05 + rng.gauss(0.0, 0.01) for _ in range(300)]
    dsr_good = es.deflated_sharpe(good, n_trials=10)
    assert 0.0 <= dsr_good <= 1.0
    # pure noise across many trials should deflate toward 0
    noise = [rng.gauss(0.0, 1.0) for _ in range(300)]
    dsr_noise = es.deflated_sharpe(noise, n_trials=500)
    assert 0.0 <= dsr_noise <= 1.0
    assert dsr_good > dsr_noise


def test_deflated_sharpe_degenerate():
    # empty / single / zero-variance must never raise, return safe 0.0
    assert es.deflated_sharpe([], n_trials=10) == 0.0
    assert es.deflated_sharpe([0.3], n_trials=10) == 0.0
    assert es.deflated_sharpe([0.2, 0.2, 0.2], n_trials=10) == 0.0


def test_day_block_perm_pvalue_signal_vs_null():
    # in_slice labels strongly higher than out-of-slice -> small p-value
    days = [chr(ord("a") + (i // 4)) for i in range(40)]   # 10 day-blocks of 4
    in_slice = [i < 20 for i in range(40)]                  # first 5 days = slice
    labels = [1.0 if i < 20 else 0.0 for i in range(40)]    # slice all 1, rest all 0
    p_sig = es.day_block_perm_pvalue(in_slice, days, labels, iters=300, seed=3)
    assert 0.0 <= p_sig <= 1.0

    # no real difference -> p-value should not be tiny
    labels_null = [1.0 if (i % 2 == 0) else 0.0 for i in range(40)]
    p_null = es.day_block_perm_pvalue(in_slice, days, labels_null, iters=300, seed=3)
    assert 0.0 <= p_null <= 1.0
    assert p_sig <= p_null


def test_day_block_perm_pvalue_deterministic_and_degenerate():
    days = ["a", "a", "b", "b"]
    in_slice = [True, True, False, False]
    labels = [1.0, 1.0, 0.0, 0.0]
    p1 = es.day_block_perm_pvalue(in_slice, days, labels, iters=200, seed=9)
    p2 = es.day_block_perm_pvalue(in_slice, days, labels, iters=200, seed=9)
    assert p1 == p2
    # degenerate: all one day-block, empty -> safe 1.0, never raises
    assert es.day_block_perm_pvalue([], [], [], iters=200, seed=9) == 1.0
    assert es.day_block_perm_pvalue([True], ["a"], [1.0], iters=200, seed=9) == 1.0


def test_two_proportion_degenerate():
    # zero-sample groups must never raise -> safe 1.0
    assert es.two_proportion_p(0, 0, 0, 0, alternative="greater") == 1.0
    assert es.two_proportion_p(5, 10, 0, 0, alternative="greater") == 1.0


def test_day_block_bootstrap_degenerate():
    # empty input must never raise -> empty list
    assert es.day_block_bootstrap([], [], lambda xs: 0.0, iters=10, seed=1) == []
