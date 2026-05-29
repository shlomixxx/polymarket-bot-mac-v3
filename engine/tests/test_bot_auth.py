"""בדיקת BOT_API_TOKEN auth middleware (FIX QA#1)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_no_token(tmp_path: Path):
    os.environ.pop("BOT_API_TOKEN", None)
    import importlib
    import main as engine_main
    importlib.reload(engine_main)
    # אחרי reload — הalert flag חוזר ל-False אבל זה לא משנה לבדיקות
    engine_main._AUTH_WARNED_NO_TOKEN = True  # לדכא warning בפלט הטסטים
    engine_main.demo.state_path = tmp_path / "demo_state.json"
    engine_main.demo.reset(10_000.0)
    engine_main.runner.rt.mode = "off"
    return TestClient(engine_main.app)


@pytest.fixture()
def client_with_token(tmp_path: Path):
    os.environ["BOT_API_TOKEN"] = "secret-token-abc"
    import importlib
    import main as engine_main
    importlib.reload(engine_main)
    engine_main._AUTH_WARNED_NO_TOKEN = True
    engine_main.demo.state_path = tmp_path / "demo_state.json"
    engine_main.demo.reset(10_000.0)
    engine_main.runner.rt.mode = "off"
    yield TestClient(engine_main.app)
    os.environ.pop("BOT_API_TOKEN", None)


# ── מצב dev (BOT_API_TOKEN לא מוגדר) ──────────────────────────────────────────

def test_dev_mode_write_succeeds_without_token(client_no_token: TestClient):
    """בלי BOT_API_TOKEN ב-ENV → כתיבות עוברות בלי header (compat dev)."""
    r = client_no_token.post("/api/demo/clear-stats")
    assert r.status_code == 200


def test_dev_mode_auth_required_endpoint(client_no_token: TestClient):
    r = client_no_token.get("/api/_auth/required")
    assert r.status_code == 200
    assert r.json() == {"token_required": False}


# ── מצב production (BOT_API_TOKEN מוגדר) ──────────────────────────────────────

def test_prod_write_without_token_returns_401(client_with_token: TestClient):
    r = client_with_token.post("/api/demo/clear-stats")
    assert r.status_code == 401, f"expected 401 without header, got {r.status_code}"
    assert "X-Bot-Token" in r.json().get("detail", "")


def test_prod_write_with_wrong_token_returns_401(client_with_token: TestClient):
    r = client_with_token.post(
        "/api/demo/clear-stats",
        headers={"X-Bot-Token": "wrong-token"},
    )
    assert r.status_code == 401


def test_prod_write_with_correct_token_returns_200(client_with_token: TestClient):
    r = client_with_token.post(
        "/api/demo/clear-stats",
        headers={"X-Bot-Token": "secret-token-abc"},
    )
    assert r.status_code == 200


def test_prod_read_endpoints_dont_require_token(client_with_token: TestClient):
    """GET ב-/api/health, /api/runtime וכו' עוברים גם בלי header."""
    r = client_with_token.get("/api/health")
    assert r.status_code == 200
    r = client_with_token.get("/api/runtime")
    assert r.status_code == 200


def test_prod_auth_required_endpoint(client_with_token: TestClient):
    r = client_with_token.get("/api/_auth/required")
    assert r.status_code == 200
    assert r.json() == {"token_required": True}


def test_prod_log_endpoint_in_whitelist(client_with_token: TestClient):
    """/api/_log/client-request חייב להישאר פתוח (frontend logging)."""
    r = client_with_token.post(
        "/api/_log/client-request",
        json={"kind": "test", "path": "/test"},
    )
    # אסור לחזיר 401 — צריך 200 או 404 או דומה (לא auth-related)
    assert r.status_code != 401


def test_live_order_requires_token_in_prod(client_with_token: TestClient):
    """ה-endpoint הקריטי ביותר — /api/live/order — חייב להידחות בלי token."""
    body = {
        "side": "Up",
        "token_id": "1234",
        "contracts": 5,
        "limit_price": 0.5,
        "order_min_size": 5.0,
    }
    r = client_with_token.post("/api/live/order", json=body)
    assert r.status_code == 401


def test_set_private_key_requires_token_in_prod(client_with_token: TestClient):
    """/api/live/private-key — חיוני שיהיה גידור."""
    r = client_with_token.post("/api/live/private-key", json={"key": "0xfake"})
    assert r.status_code == 401
