from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import data_source


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    # Isolate persisted-config writes so POST /api/data-source doesn't touch the
    # real engine/config_persisted.json (mirrors test_api_smoke.py:71-78).
    monkeypatch.setattr(main, "CONFIG_PERSISTED_PATH", tmp_path / "config_persisted.json")
    yield TestClient(main.app)
    # Reset the shared data_source singleton so other tests see the default.
    data_source.set_active("polymarket")
    main.runner.rt.config.data_source = "polymarket"


def test_get_returns_active(client):
    data_source.set_active("polymarket")
    main.runner.rt.config.data_source = "polymarket"
    assert client.get("/api/data-source").json() == {"data_source": "polymarket"}


def test_post_switches_to_binance(client):
    r = client.post("/api/data-source", json={"data_source": "binance"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "data_source": "binance"}
    assert main.runner.rt.config.data_source == "binance"
    assert data_source.get_active() == "binance"
    client.post("/api/data-source", json={"data_source": "polymarket"})  # cleanup


def test_post_invalid_rejected(client):
    assert client.post("/api/data-source", json={"data_source": "x"}).status_code == 400
