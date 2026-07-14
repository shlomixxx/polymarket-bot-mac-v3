"""Unit tests for engine/predict_secrets.py — the Predict.fun triple-lock gate.

Truth table over PREDICT_LIVE / PREDICT_WALLET_KEY / PREDICT_TESTNET, mirroring
test_binance_secrets.py's style. No network/keyring touched — env-only for M2b-step-1.
"""
from __future__ import annotations

import pytest

import predict_secrets


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("PREDICT_LIVE", "PREDICT_WALLET_KEY", "PREDICT_TESTNET"):
        monkeypatch.delenv(v, raising=False)


def test_is_testnet_default_safe(monkeypatch):
    assert predict_secrets.is_testnet() is True  # unset -> testnet (safe default)
    for off in ("0", "false", "No", "OFF", " off "):
        monkeypatch.setenv("PREDICT_TESTNET", off)
        assert predict_secrets.is_testnet() is False, off
    for other in ("1", "true", "garbage", ""):
        monkeypatch.setenv("PREDICT_TESTNET", other)
        assert predict_secrets.is_testnet() is True, other


def test_has_wallet_key(monkeypatch):
    assert predict_secrets.has_wallet_key() is False
    monkeypatch.setenv("PREDICT_WALLET_KEY", "   ")
    assert predict_secrets.has_wallet_key() is False  # whitespace-only -> absent
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc123")
    assert predict_secrets.has_wallet_key() is True


def test_is_live_enabled_requires_all_three(monkeypatch):
    # nothing set -> blocked
    assert predict_secrets.is_live_enabled() is False

    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc123")
    # key present, but PREDICT_LIVE != '1' -> still blocked
    assert predict_secrets.is_live_enabled() is False

    monkeypatch.setenv("PREDICT_LIVE", "1")
    # PREDICT_LIVE + key present, but still testnet by default -> blocked
    assert predict_secrets.is_live_enabled() is False

    monkeypatch.setenv("PREDICT_TESTNET", "0")
    # all three satisfied now
    assert predict_secrets.is_live_enabled() is True

    # remove the wallet key -> blocked again
    monkeypatch.delenv("PREDICT_WALLET_KEY", raising=False)
    assert predict_secrets.is_live_enabled() is False

    # restore key but flip PREDICT_LIVE off -> blocked
    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc123")
    monkeypatch.setenv("PREDICT_LIVE", "0")
    assert predict_secrets.is_live_enabled() is False


@pytest.mark.parametrize(
    "live,key,testnet_off,expected_reason",
    [
        (False, False, False, "PREDICT_LIVE != '1'"),
        (False, True, True, "PREDICT_LIVE != '1'"),  # PREDICT_LIVE checked first
        (True, False, True, "אין מפתח ארנק Predict.fun"),
        (True, True, False, "מצב טסטנט (PREDICT_TESTNET)"),
        (True, True, True, None),
    ],
)
def test_live_disabled_reason_names_first_open_lock(monkeypatch, live, key, testnet_off, expected_reason):
    if live:
        monkeypatch.setenv("PREDICT_LIVE", "1")
    if key:
        monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc123")
    if testnet_off:
        monkeypatch.setenv("PREDICT_TESTNET", "0")
    assert predict_secrets.live_disabled_reason() == expected_reason
    # is_live_enabled() and live_disabled_reason() must never disagree
    assert predict_secrets.is_live_enabled() == (expected_reason is None)
