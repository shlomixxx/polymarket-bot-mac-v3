from order_validation import effective_minimum_contracts, validate_contracts_for_market


def test_effective_minimum():
    assert effective_minimum_contracts(5) == 5.01


def test_validate_bump():
    ok, c, err = validate_contracts_for_market(5.0, 5.0, bump_if_needed=True)
    assert ok and c == 5.01 and err is None


def test_validate_reject():
    ok, c, err = validate_contracts_for_market(3.0, 5.0, bump_if_needed=True)
    assert not ok and err
