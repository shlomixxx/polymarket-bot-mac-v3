# engine/tests/test_data_source_config.py
"""מקור-הנתונים נשמר בקונפיג, מסונכרן למודול, ומאומת ב-API."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import data_source


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    # Redirect persisted-config path so we don't touch the real engine/config_persisted.json
    # (same pattern as test_api_smoke.py's test_decision_mode_config_default_settable_persisted).
    monkeypatch.setattr(main, "CONFIG_PERSISTED_PATH", tmp_path / "config_persisted.json")
    return TestClient(main.app)


def _full_config_body(**overrides):
    # גוף קונפיג מלא-מספיק: מתחילים מברירות-המחדל של הקונפיג הנוכחי ומעדכנים.
    body = main.ConfigBody().model_dump()
    body.update(overrides)
    return body


def test_default_data_source_is_polymarket(client):
    r = client.get("/api/strategy/config")
    assert r.status_code == 200
    assert r.json()["data_source"] == "polymarket"


def test_post_binance_persists_and_syncs_module(client):
    r = client.post("/api/strategy/config", json=_full_config_body(data_source="binance"))
    assert r.status_code == 200
    assert main.runner.rt.config.data_source == "binance"
    assert data_source.get_active() == "binance"          # module synced
    assert client.get("/api/strategy/config").json()["data_source"] == "binance"
    # cleanup: restore module + shared config singleton so we don't leak state to other tests
    client.post("/api/strategy/config", json=_full_config_body(data_source="polymarket"))
    main.runner.rt.config.data_source = "polymarket"
    data_source.set_active("polymarket")


def test_post_invalid_data_source_rejected(client):
    r = client.post("/api/strategy/config", json=_full_config_body(data_source="kraken"))
    assert r.status_code == 400
