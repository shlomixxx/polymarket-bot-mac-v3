"""Pure statistical leaves for the Edge-Watcher (recording-only / advisory).

INVARIANTS (load-bearing — see docs/superpowers/specs/2026-06-08-edge-watcher-design.md):
  * Stdlib only: math / statistics. NO scipy, NO numpy.
  * Every public fn is DEFENSIVE: degenerate input (n==0, empty lists, all-same-day,
    zero variance) returns a safe value, NEVER raises.
  * Determinism uses random.Random(seed) — NEVER the global `random` module.
  * The adversarial fixes are implemented as the *fixed* versions:
      - two_proportion_p is slice-vs-complement (two-sample), not slice-vs-constant.
      - day_block_bootstrap / day_block_perm_pvalue resample by DAY BLOCK (consecutive
        5-min windows are autocorrelated), never a plain i.i.d. binomial.

This module imports NOTHING from trading code and performs no I/O.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Hashable, Sequence


# ---------------------------------------------------------------------------
# Normal distribution: CDF + inverse-CDF (Acklam rational approximation).
# No scipy — these are accurate to ~1e-9 (CDF) / ~1e-9 (PPF, refined).
# ---------------------------------------------------------------------------


def norm_cdf(z: float) -> float:
    """Standard-normal CDF via math.erf. Safe for all real z (never raises)."""
    try:
        z = float(z)
    except (TypeError, ValueError):
        return 0.5
    if math.isnan(z):
        return 0.5
    if z == float("inf"):
        return 1.0
    if z == float("-inf"):
        return 0.0
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# Acklam's rational approximation coefficients for the inverse normal CDF.
_PPF_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_PPF_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_PPF_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_PPF_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_PPF_P_LOW = 0.02425
_PPF_P_HIGH = 1.0 - _PPF_P_LOW


def norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (quantile) via Acklam rational approximation,
    refined by one Halley step. Returns +/-inf at the boundaries; safe on bad input.
    """
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(p):
        return 0.0
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")

    if p < _PPF_P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (
            ((((_PPF_C[0] * q + _PPF_C[1]) * q + _PPF_C[2]) * q + _PPF_C[3]) * q + _PPF_C[4]) * q
            + _PPF_C[5]
        ) / ((((_PPF_D[0] * q + _PPF_D[1]) * q + _PPF_D[2]) * q + _PPF_D[3]) * q + 1.0)
    elif p <= _PPF_P_HIGH:
        q = p - 0.5
        r = q * q
        x = (
            (((((_PPF_A[0] * r + _PPF_A[1]) * r + _PPF_A[2]) * r + _PPF_A[3]) * r + _PPF_A[4]) * r + _PPF_A[5])
            * q
        ) / (((((_PPF_B[0] * r + _PPF_B[1]) * r + _PPF_B[2]) * r + _PPF_B[3]) * r + _PPF_B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(
            ((((_PPF_C[0] * q + _PPF_C[1]) * q + _PPF_C[2]) * q + _PPF_C[3]) * q + _PPF_C[4]) * q
            + _PPF_C[5]
        ) / ((((_PPF_D[0] * q + _PPF_D[1]) * q + _PPF_D[2]) * q + _PPF_D[3]) * q + 1.0)

    # One Halley refinement step to push accuracy below 1e-9.
    try:
        e = norm_cdf(x) - p
        u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
        x = x - u / (1.0 + x * u / 2.0)
    except (OverflowError, ValueError):
        pass
    return x


# ---------------------------------------------------------------------------
# Wilson score interval for a binomial proportion.
# ---------------------------------------------------------------------------


def wilson_bounds(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score (lo, hi) bounds for k successes in n trials.
    Degenerate (n<=0) -> (0.0, 0.0); never raises.
    """
    try:
        k = float(k)
        n = float(n)
        z = float(z)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if n <= 0 or not math.isfinite(n):
        return (0.0, 0.0)
    if k < 0:
        k = 0.0
    if k > n:
        k = n

    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n))
    lo = center - margin
    hi = center + margin
    return (max(0.0, lo), min(1.0, hi))


# ---------------------------------------------------------------------------
# One-sided two-proportion z-test (slice vs COMPLEMENT — never vs a constant).
# ---------------------------------------------------------------------------


def two_proportion_p(
    k1: int, n1: int, k2: int, n2: int, alternative: str = "greater"
) -> float:
    """One-sided two-proportion z-test p-value comparing p1=k1/n1 vs p2=k2/n2.

    alternative:
      "greater"  -> H1: p1 > p2
      "less"     -> H1: p1 < p2
      "two-sided"-> H1: p1 != p2
    Degenerate (n1<=0 or n2<=0, or zero pooled variance) -> 1.0; never raises.
    """
    try:
        k1 = float(k1)
        n1 = float(n1)
        k2 = float(k2)
        n2 = float(n2)
    except (TypeError, ValueError):
        return 1.0
    if n1 <= 0 or n2 <= 0 or not (math.isfinite(n1) and math.isfinite(n2)):
        return 1.0

    k1 = min(max(k1, 0.0), n1)
    k2 = min(max(k2, 0.0), n2)

    p1 = k1 / n1
    p2 = k2 / n2
    p_pool = (k1 + k2) / (n1 + n2)
    var = p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2)
    if var <= 0.0:
        # No variability (e.g. pooled rate 0 or 1) -> no evidence of a difference.
        return 1.0

    z = (p1 - p2) / math.sqrt(var)

    if alternative == "greater":
        return 1.0 - norm_cdf(z)
    if alternative == "less":
        return norm_cdf(z)
    # two-sided
    return 2.0 * (1.0 - norm_cdf(abs(z)))


# ---------------------------------------------------------------------------
# Benjamini-Hochberg FDR control -> reject mask (in the input order).
# ---------------------------------------------------------------------------


def bh_fdr(pvals: Sequence[float], q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg reject mask at FDR level q, returned in INPUT order.
    Empty input -> []; never raises.
    """
    try:
        clean: list[float] = []
        for p in pvals:
            try:
                pv = float(p)
            except (TypeError, ValueError):
                pv = 1.0
            if math.isnan(pv):
                pv = 1.0
            clean.append(min(max(pv, 0.0), 1.0))
    except TypeError:
        return []

    m = len(clean)
    if m == 0:
        return [False] * 0
    try:
        q = float(q)
    except (TypeError, ValueError):
        q = 0.10
    if not (0.0 < q <= 1.0):
        q = 0.10

    # Sort ascending, find the largest rank i with p_(i) <= (i/m) * q.
    order = sorted(range(m), key=lambda idx: clean[idx])
    k_max = -1
    for rank, idx in enumerate(order, start=1):
        if clean[idx] <= (rank / m) * q:
            k_max = rank
    mask = [False] * m
    if k_max >= 0:
        for rank, idx in enumerate(order, start=1):
            if rank <= k_max:
                mask[idx] = True
    return mask


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio (Bailey & Lopez de Prado) — multiple-trials haircut.
# ---------------------------------------------------------------------------


def deflated_sharpe(
    returns: Sequence[float], n_trials: int, gamma: float = 0.5772
) -> float:
    """Probability the observed Sharpe is genuinely > the expected-max-under-null
    Sharpe after `n_trials` independent trials. Returns a value in [0, 1].

    Degenerate (n<2, zero variance, n_trials<1) -> 0.0; never raises.
    """
    try:
        vals = [float(r) for r in returns]
    except (TypeError, ValueError):
        return 0.0
    n = len(vals)
    if n < 2:
        return 0.0
    try:
        n_trials = int(n_trials)
    except (TypeError, ValueError):
        n_trials = 1
    if n_trials < 1:
        n_trials = 1

    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    # Treat numerically-zero variance (all-equal returns) as degenerate.
    scale = max(abs(mean), 1.0)
    if var <= (1e-12 * scale) ** 2:
        return 0.0
    sd = math.sqrt(var)
    sr = mean / sd  # observed (per-trade) Sharpe

    # Skewness & kurtosis of the return stream (used in the DSR variance term).
    m2 = sum((v - mean) ** 2 for v in vals) / n
    m3 = sum((v - mean) ** 3 for v in vals) / n
    m4 = sum((v - mean) ** 4 for v in vals) / n
    if m2 <= 0.0:
        return 0.0
    skew = m3 / (m2 ** 1.5)
    kurt = m4 / (m2 ** 2)  # non-excess kurtosis

    # Expected maximum Sharpe under the null across n_trials independent trials.
    if n_trials <= 1:
        sr0 = 0.0
    else:
        try:
            e_inv = math.e ** -1
            z1 = norm_ppf(1.0 - 1.0 / n_trials)
            z2 = norm_ppf(1.0 - 1.0 / n_trials * e_inv)
        except (ValueError, OverflowError):
            return 0.0
        if not (math.isfinite(z1) and math.isfinite(z2)):
            return 0.0
        sr0 = (1.0 - gamma) * z1 + gamma * z2  # expected max Sharpe under null

    # Standard error of the Sharpe estimator (Lo / Mertens), with higher moments.
    denom = n - 1
    if denom <= 0:
        return 0.0
    sr_var = (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / denom
    if sr_var <= 0.0:
        # Fall back to the classic 1/(n-1) variance form.
        sr_var = 1.0 / denom
    sr_se = math.sqrt(sr_var)
    if sr_se <= 0.0:
        return 0.0

    dsr = norm_cdf((sr - sr0) / sr_se)
    return min(max(dsr, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Day-block resampling helpers (kill the i.i.d. assumption — fixes C2/I2).
# ---------------------------------------------------------------------------


def _group_by_day(
    day_keys: Sequence[Hashable],
) -> tuple[list[Hashable], dict[Hashable, list[int]]]:
    """Group row indices by their day key, preserving first-seen day order."""
    order: list[Hashable] = []
    groups: dict[Hashable, list[int]] = {}
    for i, d in enumerate(day_keys):
        if d not in groups:
            groups[d] = []
            order.append(d)
        groups[d].append(i)
    return order, groups


def day_block_bootstrap(
    values: Sequence[float],
    day_keys: Sequence[Hashable],
    stat_fn: Callable[[list[float]], float],
    iters: int = 1000,
    seed: int = 0,
) -> list[float]:
    """Block-bootstrap `stat_fn` over `values`, resampling whole DAY BLOCKS with
    replacement (consecutive same-day windows share a regime, so they move together).

    Deterministic for a fixed seed (uses random.Random(seed), never global random).
    Returns `iters` bootstrap statistics. Empty/degenerate input -> []; never raises.
    """
    try:
        vals = [float(v) for v in values]
    except (TypeError, ValueError):
        return []
    n = len(vals)
    if n == 0 or len(day_keys) != n:
        return []
    try:
        iters = int(iters)
    except (TypeError, ValueError):
        iters = 0
    if iters <= 0:
        return []

    day_order, groups = _group_by_day(day_keys)
    n_days = len(day_order)
    if n_days == 0:
        return []

    rng = random.Random(seed)
    out: list[float] = []
    for _ in range(iters):
        resampled: list[float] = []
        # Resample n_days blocks with replacement to keep total ~constant.
        for _ in range(n_days):
            d = day_order[rng.randrange(n_days)]
            for idx in groups[d]:
                resampled.append(vals[idx])
        try:
            stat = float(stat_fn(resampled)) if resampled else 0.0
        except (ZeroDivisionError, ValueError, TypeError):
            stat = 0.0
        out.append(stat)
    return out


def day_block_perm_pvalue(
    in_slice: Sequence[bool],
    day_keys: Sequence[Hashable],
    labels: Sequence[float],
    iters: int = 1000,
    seed: int = 0,
) -> float:
    """One-sided permutation p-value for "slice mean(label) > complement mean(label)",
    permuting the slice/complement assignment at the WHOLE-DAY level (so autocorrelated
    same-day windows are kept together — a single lucky day cannot manufacture a slice).

    Deterministic for a fixed seed. Degenerate input (empty, single day-block, length
    mismatch) -> 1.0; never raises.
    """
    n = len(labels)
    if n == 0 or len(in_slice) != n or len(day_keys) != n:
        return 1.0
    try:
        lab = [float(x) for x in labels]
    except (TypeError, ValueError):
        return 1.0
    try:
        iters = int(iters)
    except (TypeError, ValueError):
        iters = 0
    if iters <= 0:
        return 1.0

    day_order, groups = _group_by_day(day_keys)
    n_days = len(day_order)
    if n_days < 2:
        # Can't permute day assignment with fewer than 2 day-blocks.
        return 1.0

    # Per-day: which days are (predominantly) in the slice, plus each day's labels.
    day_in_slice: list[bool] = []
    day_label_sums: list[float] = []
    day_label_counts: list[int] = []
    for d in day_order:
        idxs = groups[d]
        in_cnt = sum(1 for i in idxs if bool(in_slice[i]))
        day_in_slice.append(in_cnt * 2 >= len(idxs))  # majority -> treated as slice day
        day_label_sums.append(sum(lab[i] for i in idxs))
        day_label_counts.append(len(idxs))

    n_slice_days = sum(1 for b in day_in_slice if b)
    if n_slice_days == 0 or n_slice_days == n_days:
        # Degenerate split across day-blocks -> no comparison possible.
        return 1.0

    def _slice_minus_complement(assignment: list[bool]) -> float:
        s_sum = s_cnt = c_sum = c_cnt = 0.0
        for j, is_slice_day in enumerate(assignment):
            if is_slice_day:
                s_sum += day_label_sums[j]
                s_cnt += day_label_counts[j]
            else:
                c_sum += day_label_sums[j]
                c_cnt += day_label_counts[j]
        s_mean = s_sum / s_cnt if s_cnt > 0 else 0.0
        c_mean = c_sum / c_cnt if c_cnt > 0 else 0.0
        return s_mean - c_mean

    observed = _slice_minus_complement(day_in_slice)

    rng = random.Random(seed)
    day_indices = list(range(n_days))
    ge = 0
    for _ in range(iters):
        rng.shuffle(day_indices)
        # First n_slice_days shuffled day-blocks become the permuted slice.
        perm_assignment = [False] * n_days
        for j in day_indices[:n_slice_days]:
            perm_assignment[j] = True
        if _slice_minus_complement(perm_assignment) >= observed:
            ge += 1

    # +1 / +1 small-sample correction (Phipson & Smyth).
    return (ge + 1) / (iters + 1)


# ---------------------------------------------------------------------------
# Tertile cut-points.
# ---------------------------------------------------------------------------


def tertiles(values: Sequence[float]) -> tuple[float, float]:
    """Return (q33, q66) tertile cut-points. Empty/degenerate -> (0.0, 0.0) and
    always q33 <= q66; never raises.
    """
    try:
        vals = sorted(float(v) for v in values if v is not None)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    n = len(vals)
    if n == 0:
        return (0.0, 0.0)
    if n == 1:
        return (vals[0], vals[0])

    def _quantile(q: float) -> float:
        # Linear-interpolation quantile on the sorted sample.
        pos = q * (n - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return vals[lo]
        frac = pos - lo
        return vals[lo] * (1.0 - frac) + vals[hi] * frac

    q33 = _quantile(1.0 / 3.0)
    q66 = _quantile(2.0 / 3.0)
    if q33 > q66:
        q33, q66 = q66, q33
    return (q33, q66)
