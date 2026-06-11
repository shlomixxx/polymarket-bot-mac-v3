"""Smoke tests for the Binance cockpit HTTP endpoints (Task 3 wiring).

NO real network and NO real keys: every test monkeypatches
`main._get_binance_client` to return a tiny fake client and (where needed)
patches `main.binance_cockpit` functions, so the FastAPI layer is exercised
end-to-end while touching neither the exchange nor secret_store keyring.

It proves the SAFETY wiring:
  * GET /api/binance/state is read-only and NEVER returns key material;
  * the BINANCE_LIVE gate is honoured — with live OFF, /trade runs against a
    FORCED testnet client and is clearly labelled live_enabled=false (a real
    order is never placed silently);
  * the global X-Bot-Token middleware protects every binance POST;
  * the naked-position guard (NakedPositionError) surfaces as a 409, not a 500;
  * /preview places nothing and /close flattens via the cockpit.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# A minimal fake exchange client (duck-typed to what the endpoints call).
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, *, balance: float = 1000.0):
        self._balance = balance

    def get_balance(self, asset: str = "USDT"):
        return self._balance

    def get_position(self, symbol: str):
        return {"symbol": symbol, "qty": 0.0, "entry_price": 0.0,
                "side": "flat", "leverage": 3.0, "unrealized_pnl": 0.0}

    def get_liquidation_price(self, symbol: str):
        return None


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """Isolate demo state + force live OFF + testnet for the whole module."""
    import main as engine_main

    engine_main.demo.state_path = tmp_path / "demo_state.json"
    engine_main.demo.reset(10_000.0)
    engine_main.runner.rt.mode = "off"

    # Default-safe posture: live OFF, testnet ON, no BOT_API_TOKEN (dev/open).
    monkeypatch.delenv("BINANCE_LIVE", raising=False)
    monkeypatch.delenv("BOT_API_TOKEN", raising=False)
    monkeypatch.setenv("USE_TESTNET", "true")

    # Never build a real client / touch the network.
    fake = FakeClient()
    monkeypatch.setattr(engine_main, "_get_binance_client",
                        lambda *, force_testnet=False: fake)

    return engine_main, TestClient(engine_main.app), fake, monkeypatch


# ---------------------------------------------------------------------------
# GET /api/binance/state — read-only, no keys leaked.
# ---------------------------------------------------------------------------

def test_state_readonly_never_leaks_keys(env):
    engine_main, client, _fake, _mp = env
    r = client.get("/api/binance/state?symbol=BTCUSDT")
    assert r.status_code == 200
    j = r.json()
    assert j["symbol"] == "BTCUSDT"
    assert j["balance_usdt"] == 1000.0
    assert j["live"]["live_enabled"] is False
    assert j["live"]["testnet"] is True
    assert j["caps"]["allow_new"] is True  # day/drawdown 0 -> caps not breached
    # No key material anywhere in the serialized response.
    blob = r.text.lower()
    assert "api_key" not in blob and "api_secret" not in blob and "secret" not in blob


# ---------------------------------------------------------------------------
# POST /api/binance/preview — places nothing; routes to cockpit.preview_trade.
# ---------------------------------------------------------------------------

def test_preview_calls_cockpit_with_account_equity(env):
    engine_main, client, _fake, mp = env
    seen: dict[str, Any] = {}

    def _fake_preview(_client, **kw):
        seen.update(kw)
        return {"approved": True, "qty": 0.01, "checks": []}

    mp.setattr(engine_main.binance_cockpit, "preview_trade", _fake_preview)

    body = {"symbol": "btcusdt", "side": "long", "entry": 60000,
            "stop": 59000, "target": 62000, "leverage": 3}
    r = client.post("/api/binance/preview", json=body)
    assert r.status_code == 200
    assert r.json()["approved"] is True
    # equity was pulled from the account balance and symbol upper-cased.
    assert seen["equity"] == 1000.0
    assert seen["symbol"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# POST /api/binance/trade — BINANCE_LIVE gate: live OFF -> forced testnet,
# labelled, never a real order.
# ---------------------------------------------------------------------------

def test_trade_live_disabled_forces_testnet_and_labels(env):
    engine_main, client, _fake, mp = env
    forced: dict[str, Any] = {}

    def _spy_get_client(*, force_testnet: bool = False):
        forced["force_testnet"] = force_testnet
        return FakeClient()

    mp.setattr(engine_main, "_get_binance_client", _spy_get_client)
    mp.setattr(engine_main.binance_cockpit, "place_manual_trade",
               lambda c, p, risk_state=None: {"ok": True, "placed_order": True,
                                              "symbol": p["symbol"], "qty": 0.01})

    body = {"symbol": "BTCUSDT", "side": "long", "entry": 60000,
            "stop": 59000, "leverage": 3}
    r = client.post("/api/binance/trade", json=body)
    assert r.status_code == 200
    j = r.json()
    # live OFF -> the client was FORCED to testnet, response clearly labelled.
    assert forced["force_testnet"] is True
    assert j["live_enabled"] is False
    assert j["testnet"] is True
    assert "TESTNET" in j["note"]


def test_trade_naked_guard_returns_409(env):
    engine_main, client, _fake, mp = env

    def _raise(_c, _p, risk_state=None):
        raise engine_main.binance_cockpit.NakedPositionError("stop unverified -> flattened")

    mp.setattr(engine_main.binance_cockpit, "place_manual_trade", _raise)
    body = {"symbol": "BTCUSDT", "side": "long", "entry": 60000, "stop": 59000}
    r = client.post("/api/binance/trade", json=body)
    assert r.status_code == 409
    j = r.json()
    assert j["naked_position_guard"] is True
    assert j["ok"] is False


# ---------------------------------------------------------------------------
# POST /api/binance/close — flattens via the cockpit.
# ---------------------------------------------------------------------------

def test_close_routes_to_cockpit(env):
    engine_main, client, _fake, mp = env
    mp.setattr(engine_main.binance_cockpit, "close_position",
               lambda c, s: {"ok": True, "symbol": s, "flat": True})
    r = client.post("/api/binance/close", json={"symbol": "BTCUSDT"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["symbol"] == "BTCUSDT"
    assert j["live_enabled"] is False


# ---------------------------------------------------------------------------
# Global X-Bot-Token middleware protects the binance POSTs.
# ---------------------------------------------------------------------------

def test_trade_requires_bot_token_when_configured(env):
    engine_main, client, _fake, mp = env
    mp.setenv("BOT_API_TOKEN", "secret-token")
    # Avoid actually trading even if auth slipped through.
    mp.setattr(engine_main.binance_cockpit, "place_manual_trade",
               lambda c, p, risk_state=None: {"ok": True})

    body = {"symbol": "BTCUSDT", "side": "long", "entry": 60000, "stop": 59000}
    # Missing token -> 401 from the global middleware.
    assert client.post("/api/binance/trade", json=body).status_code == 401
    # Correct token -> allowed through.
    r = client.post("/api/binance/trade", json=body,
                    headers={"X-Bot-Token": "secret-token"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Boot-time reconcile guard: no-op (and no crash) when live is disabled.
# ---------------------------------------------------------------------------

def test_reconcile_on_start_noop_when_live_disabled(env):
    engine_main, _client, _fake, mp = env
    called = {"n": 0}
    mp.setattr(engine_main.binance_cockpit, "reconcile_on_start",
               lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    # live disabled -> must NOT call into the cockpit, must NOT raise.
    engine_main._binance_reconcile_on_start()
    assert called["n"] == 0
