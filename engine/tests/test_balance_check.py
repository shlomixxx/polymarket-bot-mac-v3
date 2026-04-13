"""Tests for check_balance_before_order() in live_clob.py."""
from unittest.mock import patch

from live_clob import check_balance_before_order


def _mock_account(balance_usd, ok=True, error=None):
    result = {"ok": ok, "balance_usd": balance_usd, "allowance_usd": balance_usd}
    if error:
        result["error"] = error
    return result


@patch("live_clob.fetch_polymarket_clob_account")
def test_sufficient_balance(mock_acct):
    mock_acct.return_value = _mock_account(10.0)
    ok, err = check_balance_before_order(5.0)
    assert ok is True
    assert err is None


@patch("live_clob.fetch_polymarket_clob_account")
def test_insufficient_balance(mock_acct):
    mock_acct.return_value = _mock_account(2.0)
    ok, err = check_balance_before_order(5.0)
    assert ok is False
    assert "2.00" in err
    assert "5.00" in err


@patch("live_clob.fetch_polymarket_clob_account")
def test_zero_balance_eoa(mock_acct):
    mock_acct.return_value = _mock_account(0.0)
    with patch.dict("os.environ", {"POLYMARKET_SIGNATURE_TYPE": "0"}):
        ok, err = check_balance_before_order(1.0)
    assert ok is False
    assert "POLYMARKET_SIGNATURE_TYPE" in err
    assert "CLOB" in err


@patch("live_clob.fetch_polymarket_clob_account")
def test_zero_balance_proxy(mock_acct):
    mock_acct.return_value = _mock_account(0.0)
    with patch.dict("os.environ", {"POLYMARKET_SIGNATURE_TYPE": "1"}):
        ok, err = check_balance_before_order(1.0)
    assert ok is False
    assert "POLYMARKET_SIGNATURE_TYPE" not in err
    assert "CLOB" in err


@patch("live_clob.fetch_polymarket_clob_account")
def test_account_error(mock_acct):
    mock_acct.return_value = _mock_account(None, ok=False, error="connection failed")
    ok, err = check_balance_before_order(1.0)
    assert ok is False
    assert "connection failed" in err


@patch("live_clob.fetch_polymarket_clob_account")
def test_balance_none(mock_acct):
    mock_acct.return_value = _mock_account(None)
    ok, err = check_balance_before_order(1.0)
    assert ok is False
    assert "לא ניתן לקרוא" in err
