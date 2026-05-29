"""בדיקת clamp של limit_price ב-/api/live/order (FIX QA#3).

לפני התיקון: limit_price=10.0 היה מתקבל ומעובד.
אחרי התיקון: כל ערך מחוץ ל-[0.01, 0.99] מחזיר 400.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    import main as engine_main
    engine_main.demo.state_path = tmp_path / "demo_state.json"
    engine_main.demo.reset(10_000.0)
    engine_main.runner.rt.mode = "off"
    return TestClient(engine_main.app)


def _order_body(price: float) -> dict:
    return {
        "side": "Up",
        "token_id": "1234567890",
        "contracts": 5,
        "limit_price": price,
        "order_min_size": 5.0,
    }


def test_price_above_max_rejected(client: TestClient):
    r = client.post("/api/live/order", json=_order_body(10.0))
    assert r.status_code == 400, f"expected 400 for price=10.0, got {r.status_code}"
    detail = r.json().get("detail", "")
    assert "limit_price" in detail and "0.01" in detail and "0.99" in detail


def test_price_below_min_rejected(client: TestClient):
    r = client.post("/api/live/order", json=_order_body(0.005))
    assert r.status_code == 400, f"expected 400 for price=0.005, got {r.status_code}"


def test_negative_price_rejected(client: TestClient):
    r = client.post("/api/live/order", json=_order_body(-0.5))
    assert r.status_code == 400


def test_zero_price_becomes_default_then_passes_clamp(client: TestClient):
    """limit_price=0 → `or 0.5` בקוד הופך אותו ל-0.5, וזה בטווח חוקי."""
    # נכשל ב-no-private-key אחרי clamp, לא ב-clamp עצמו → 400 או הודעה אחרת.
    # מספיק לוודא שזה לא 400 של "limit_price חייב להיות".
    r = client.post("/api/live/order", json=_order_body(0.0))
    if r.status_code == 400:
        detail = r.json().get("detail", "")
        assert "limit_price חייב" not in detail, (
            "price=0 נכשל ב-clamp במקום להפוך ל-0.5"
        )


def test_valid_price_passes_clamp_check(client: TestClient):
    """limit_price=0.5 (חוקי) — אם נדחה זה לא ב-clamp.

    יכול להחזיר 400 בגלל "חסר POLYMARKET_PRIVATE_KEY" או דומה, אבל לא בגלל clamp.
    """
    r = client.post("/api/live/order", json=_order_body(0.5))
    if r.status_code == 400:
        detail = r.json().get("detail", "")
        assert "limit_price חייב" not in detail
