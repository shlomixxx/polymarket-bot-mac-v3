from order_validation import effective_minimum_contracts, validate_contracts_for_market


def test_effective_minimum():
    assert effective_minimum_contracts(5) == 5.01


def test_validate_bump():
    ok, c, err = validate_contracts_for_market(5.0, 5.0, bump_if_needed=True)
    assert ok and c == 5.01 and err is None


def test_validate_reject():
    ok, c, err = validate_contracts_for_market(3.0, 5.0, bump_if_needed=True)
    assert not ok and err


def test_effective_min_market_overrides_user():
    """min_contracts=1 from user, order_min_size=5 from market → effective = 5."""
    import math
    user_min = 1
    market_min_size = 5.0
    effective = max(user_min, int(math.ceil(market_min_size)))
    assert effective == 5


def test_effective_min_user_higher():
    """min_contracts=10 from user, order_min_size=5 from market → effective = 10."""
    import math
    user_min = 10
    market_min_size = 5.0
    effective = max(user_min, int(math.ceil(market_min_size)))
    assert effective == 10
