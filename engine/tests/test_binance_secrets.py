"""Unit tests for engine/binance_secrets.py — the Binance key store + live gate.

No real keyring is touched: secret_store is patched with an in-memory fake so the
roundtrip is exercised purely in process. Proves:
  * save/load roundtrip uses the DEDICATED service ('binance-futures-bot'), never
    the polymarket store;
  * keys are stored as the "KEY\\nSECRET" blob binance_exchange reads back;
  * env vars take precedence over the store;
  * is_live_enabled() requires ALL of: BINANCE_LIVE=='1' + keys + not testnet;
  * live_status() exposes only booleans + a reason, NEVER key material.
"""
from __future__ import annotations

import pytest

import binance_secrets


class _FakeStore:
    """In-memory stand-in for secret_store, keyed by service."""
    def __init__(self):
        self.data: dict[str, str] = {}

    def save_key(self, key: str, service: str = "polymarket-bot"):
        self.data[service] = key
        return True

    def load_key(self, service: str = "polymarket-bot"):
        return self.data.get(service)

    def delete_key(self, service: str = "polymarket-bot"):
        return self.data.pop(service, None) is not None


@pytest.fixture()
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(binance_secrets, "secret_store", fake)
    # Clean env for deterministic gate behaviour.
    for v in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_LIVE", "USE_TESTNET"):
        monkeypatch.delenv(v, raising=False)
    return fake


def test_save_load_roundtrip_uses_dedicated_service(store):
    assert binance_secrets.save_keys("KEYabc", "SECRETxyz") is True
    # stored under the binance service only — NOT the polymarket store
    assert "binance-futures-bot" in store.data
    assert "polymarket-bot" not in store.data
    # stored as the KEY\nSECRET blob the exchange reads back
    assert store.data["binance-futures-bot"] == "KEYabc\nSECRETxyz"
    assert binance_secrets.load_keys() == ("KEYabc", "SECRETxyz")
    assert binance_secrets.has_keys() is True


def test_save_refuses_blanks_and_newlines(store):
    assert binance_secrets.save_keys("", "x") is False
    assert binance_secrets.save_keys("x", "") is False
    assert binance_secrets.save_keys("a\nb", "x") is False
    assert binance_secrets.has_keys() is False


def test_env_takes_precedence_over_store(store, monkeypatch):
    binance_secrets.save_keys("STOREKEY", "STORESECRET")
    monkeypatch.setenv("BINANCE_API_KEY", "ENVKEY")
    monkeypatch.setenv("BINANCE_API_SECRET", "ENVSECRET")
    assert binance_secrets.load_keys() == ("ENVKEY", "ENVSECRET")


def test_is_testnet_default_safe(store, monkeypatch):
    assert binance_secrets.is_testnet() is True            # unset -> testnet
    monkeypatch.setenv("USE_TESTNET", "off")
    assert binance_secrets.is_testnet() is False
    monkeypatch.setenv("USE_TESTNET", "garbage")
    assert binance_secrets.is_testnet() is True            # unknown -> stay safe


def test_is_live_enabled_requires_all_three(store, monkeypatch):
    binance_secrets.save_keys("K", "S")
    # keys present but BINANCE_LIVE != 1 -> blocked
    assert binance_secrets.is_live_enabled() is False
    monkeypatch.setenv("BINANCE_LIVE", "1")
    # still testnet by default -> blocked
    assert binance_secrets.is_live_enabled() is False
    monkeypatch.setenv("USE_TESTNET", "off")
    # all three now satisfied
    assert binance_secrets.is_live_enabled() is True
    # remove keys -> blocked again
    store.delete_key(service="binance-futures-bot")
    assert binance_secrets.is_live_enabled() is False


def test_live_status_exposes_only_booleans(store, monkeypatch):
    binance_secrets.save_keys("SUPERSECRETKEY", "SUPERSECRETSECRET")
    st = binance_secrets.live_status()
    assert set(st) == {"live_enabled", "binance_live_flag", "testnet",
                       "has_keys", "reason_blocked"}
    # no key material leaks into the status
    blob = str(st)
    assert "SUPERSECRETKEY" not in blob and "SUPERSECRETSECRET" not in blob
    assert st["live_enabled"] is False  # testnet by default
    assert st["has_keys"] is True


def test_delete_keys(store):
    binance_secrets.save_keys("K", "S")
    assert binance_secrets.delete_keys() is True
    assert binance_secrets.has_keys() is False
