"""טסטים ל-memoization של ה-CLOB trading client (A-6) — חוסך auth handshake בכל order/poll."""
from unittest.mock import MagicMock, patch

import pytest

import live_clob


@pytest.fixture(autouse=True)
def _reset_cache():
    live_clob.reset_trading_client_cache()
    yield
    live_clob.reset_trading_client_cache()


def _patch_clobclient(constructed, *, creds_raises=False):
    def make_client(*a, **k):
        constructed["n"] += 1
        m = MagicMock()
        if creds_raises:
            m.create_or_derive_api_creds.side_effect = RuntimeError("auth down")
        else:
            m.create_or_derive_api_creds.return_value = {"key": "k"}
        m.get_address.return_value = "0xSIGNER"
        return m
    return patch("py_clob_client.client.ClobClient", side_effect=make_client)


def test_trading_client_memoized(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.delenv("POLYMARKET_LIVE", raising=False)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")
    monkeypatch.delenv("POLYMARKET_FUNDER", raising=False)

    constructed = {"n": 0}
    with _patch_clobclient(constructed):
        c1, e1 = live_clob.build_trading_client()
        c2, e2 = live_clob.build_trading_client()

    assert e1 is None and e2 is None
    assert c1 is c2  # אותו client בדיוק — ממומואיז
    # build בודד בונה ClobClient פעמיים (temp + final); קריאה שנייה מ-cache => סה"כ 2, לא 4
    assert constructed["n"] == 2


def test_reset_forces_rebuild(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.delenv("POLYMARKET_LIVE", raising=False)
    constructed = {"n": 0}
    with _patch_clobclient(constructed):
        live_clob.build_trading_client()
        live_clob.reset_trading_client_cache()
        live_clob.build_trading_client()
    assert constructed["n"] == 4  # שני build מלאים


def test_kill_switch_not_cached(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("POLYMARKET_LIVE", "0")
    c, e = live_clob.build_trading_client()
    assert c is None
    assert e  # סיבת kill-switch


def test_creds_failure_not_cached(monkeypatch):
    """כשל auth לא נשמר ב-cache — קריאה הבאה מנסה מחדש (לא ננעל לכל חיי התהליך)."""
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.delenv("POLYMARKET_LIVE", raising=False)

    fail = {"n": 0}
    with _patch_clobclient(fail, creds_raises=True):
        c1, e1 = live_clob.build_trading_client()
    assert c1 is None and e1

    ok = {"n": 0}
    with _patch_clobclient(ok, creds_raises=False):
        c2, e2 = live_clob.build_trading_client()
    assert c2 is not None and e2 is None  # נבנה מחדש, לא הוגש כשל מ-cache
