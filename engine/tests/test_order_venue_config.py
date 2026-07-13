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
