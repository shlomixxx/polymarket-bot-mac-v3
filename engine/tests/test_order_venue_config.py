# engine/tests/test_order_venue_config.py
"""order_venue נשמר בקונפיג, מסונכרן ל-runner._venue, ומאומת ב-API (מראה את data_source)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PERSISTED_PATH", tmp_path / "config_persisted.json")
    yield TestClient(main.app)
    main.runner.rt.config.order_venue = "polymarket"
    main.runner.select_venue("polymarket")


def test_default_is_polymarket(client):
    assert client.get("/api/strategy/config").json()["order_venue"] == "polymarket"
    assert client.get("/api/order-venue").json() == {"order_venue": "polymarket"}


def test_post_endpoint_switches_and_selects_venue(client):
    r = client.post("/api/order-venue", json={"order_venue": "predict_fun"})
    assert r.status_code == 200 and r.json() == {"ok": True, "order_venue": "predict_fun"}
    assert main.runner.rt.config.order_venue == "predict_fun"
    assert main.runner._venue.name == "predict_fun"


def test_invalid_rejected(client):
    assert client.post("/api/order-venue", json={"order_venue": "kraken"}).status_code == 400


def test_partial_config_save_keeps_order_venue(client):
    client.post("/api/order-venue", json={"order_venue": "predict_fun"})
    body = main.ConfigBody().model_dump(); body.pop("order_venue", None)
    assert client.post("/api/strategy/config", json=body).status_code == 200
    assert main.runner.rt.config.order_venue == "predict_fun"   # NOT reverted


def test_min_contracts_floor_reads_active_venue_not_hardcoded_polymarket(client, monkeypatch):
    """M2b: _clamp_min_contracts_to_market_floor() must read the market floor from the
    ACTIVE venue (runner.venue.discover_active_window), not the hardcoded module-level
    (Polymarket-only) discover_active_btc_window — else a switch to Predict.fun would be
    clamped against Polymarket's floor instead of Predict.fun's real 1 USDT minimum."""

    class _PolymarketMarket:
        order_min_size = 999.0  # a wildly-wrong floor if this were consulted for predict_fun

    async def _fake_polymarket_discover(_window):
        return _PolymarketMarket()

    # Hardcoded Polymarket-specific module function reports a bogus HIGH floor.
    monkeypatch.setattr(main, "discover_active_btc_window", _fake_polymarket_discover)

    r = client.post("/api/order-venue", json={"order_venue": "predict_fun"})
    assert r.status_code == 200

    class _PredictFunMarket:
        order_min_size = 1.0  # Predict.fun's real flat 1 USDT minimum

    async def _fake_predict_fun_discover(_window):
        return _PredictFunMarket()

    monkeypatch.setattr(main.runner.venue, "discover_active_window", _fake_predict_fun_discover)

    # NOTE: model_dump() bakes in every field including order_venue's default
    # ("polymarket"); pop it so this partial save can't silently switch the venue back
    # (same pattern as test_partial_config_save_keeps_order_venue above).
    body = main.ConfigBody(min_contracts=1).model_dump()
    body.pop("order_venue", None)
    r = client.post("/api/strategy/config", json=body)
    assert r.status_code == 200
    # Must reflect the ACTIVE venue's floor (1) — NOT the hardcoded polymarket 999.
    assert main.runner.rt.config.min_contracts == 1


def test_cannot_select_predict_fun_while_live(client):
    main.runner.rt.live_trading = True
    try:
        r = client.post("/api/order-venue", json={"order_venue": "predict_fun"})
        assert r.status_code == 400
        assert main.runner.rt.config.order_venue == "polymarket"   # unchanged
        assert main.runner._venue.name == "polymarket"
        # config-POST path also guarded
        body = main.ConfigBody(order_venue="predict_fun").model_dump()
        assert client.post("/api/strategy/config", json=body).status_code == 400
    finally:
        main.runner.rt.live_trading = False
