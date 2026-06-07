"""
Tests for the renormalized weighted_score in signal_engine.

THE BUG: compute_signals summed ta_norm*W.ta + clob_norm*W.clob + ... where the
WEIGHTS sum to 1.0. The strategy loop calls compute_signals WITHOUT order books, so
clob_result.available is always False -> clob_norm=0 but its 0.30 weight stayed in the
implicit denominator, silently scaling every weighted_score by ~0.70x.

THE FIX: _renormalized_score includes a component's weight in BOTH numerator and
denominator ONLY when that component is available.
"""
from signal_engine import WEIGHTS, _renormalized_score


def test_all_available_equals_old_flat_weighted_sum():
    # When every component is available, since WEIGHTS sum to 1.0 the renormalized
    # score must equal the old flat weighted sum (denominator == 1.0).
    ta_norm, clob_norm, history_norm, sentiment_norm = 0.8, -0.4, 0.2, -0.1
    parts = [
        (ta_norm, WEIGHTS["ta"], True),
        (clob_norm, WEIGHTS["clob"], True),
        (history_norm, WEIGHTS["history"], True),
        (sentiment_norm, WEIGHTS["sentiment"], True),
    ]
    old_flat = (
        ta_norm * WEIGHTS["ta"]
        + clob_norm * WEIGHTS["clob"]
        + history_norm * WEIGHTS["history"]
        + sentiment_norm * WEIGHTS["sentiment"]
    )
    assert _renormalized_score(parts) == round(old_flat, 4)


def test_clob_unavailable_renormalizes_over_remaining_weights():
    # The real production case: CLOB missing. The other three renormalize over
    # (1.0 - 0.30) = 0.70, producing a STRICTLY LARGER magnitude than the old
    # diluted value for the same norms.
    ta_norm, history_norm, sentiment_norm = 0.5, 0.5, 0.5
    parts = [
        (ta_norm, WEIGHTS["ta"], True),
        (0.0, WEIGHTS["clob"], False),  # clob unavailable -> norm 0.0
        (history_norm, WEIGHTS["history"], True),
        (sentiment_norm, WEIGHTS["sentiment"], True),
    ]
    new_score = _renormalized_score(parts)

    old_diluted = (
        ta_norm * WEIGHTS["ta"]
        + 0.0 * WEIGHTS["clob"]
        + history_norm * WEIGHTS["history"]
        + sentiment_norm * WEIGHTS["sentiment"]
    )
    # All three norms positive -> new score is strictly larger than the diluted one.
    assert new_score > round(old_diluted, 4)

    expected_num = (
        ta_norm * WEIGHTS["ta"]
        + history_norm * WEIGHTS["history"]
        + sentiment_norm * WEIGHTS["sentiment"]
    )
    expected_den = WEIGHTS["ta"] + WEIGHTS["history"] + WEIGHTS["sentiment"]
    assert new_score == round(expected_num / expected_den, 4)


def test_single_available_component_equals_its_own_norm():
    # Only TA available with ta_norm = -0.5 -> weighted_score == -0.5 (NOT -0.5*0.4).
    parts = [
        (-0.5, WEIGHTS["ta"], True),
        (0.0, WEIGHTS["clob"], False),
        (0.0, WEIGHTS["history"], False),
        (0.0, WEIGHTS["sentiment"], False),
    ]
    assert _renormalized_score(parts) == -0.5


def test_nothing_available_returns_zero_no_divide_by_zero():
    parts = [
        (0.0, WEIGHTS["ta"], False),
        (0.0, WEIGHTS["clob"], False),
        (0.0, WEIGHTS["history"], False),
        (0.0, WEIGHTS["sentiment"], False),
    ]
    assert _renormalized_score(parts) == 0.0
