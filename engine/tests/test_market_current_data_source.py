"""Task 5: /api/market/current מנתב את "מחיר לנצח" לפי מקור-הנתונים הפעיל.

בדיקות אינטגרציה מעל ה-handler עצמו (לא רק ה-helper הטהור): מוודאות שבמצב binance
מוחזר price_to_beat_source == "binance_1m" ו-chainlink_stream.get_price_to_beat
כלל לא נקרא, ושבמצב polymarket ההתנהגות הקיימת (Chainlink מועדף) לא השתנתה.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import data_source
import main as engine_main


def _fake_market(epoch: int = 1_700_000_000) -> SimpleNamespace:
    return SimpleNamespace(
        slug="btc-updown-test",
        epoch=epoch,
        title="BTC Up/Down",
        token_up="tok-up",
        token_down="tok-down",
        outcome_prices=[0.5, 0.5],
        order_min_size=5.0,
        order_min_size_source="gamma",
        window_sec=300,
        resolution_source="https://example.invalid",
    )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine_main, "CONFIG_PERSISTED_PATH", tmp_path / "config_persisted.json")
    # Force a fresh epoch each test so the module-level cached_open/cached_ptb_source
    # (process-global) don't leak state between tests.
    engine_main.last_epoch_for_open = None
    yield TestClient(engine_main.app)
    data_source.set_active("polymarket")
    engine_main.runner.rt.config.data_source = "polymarket"
    engine_main.last_epoch_for_open = None


def test_binance_mode_uses_binance_open_and_skips_chainlink(client: TestClient, monkeypatch):
    data_source.set_active("binance")
    engine_main.runner.rt.config.data_source = "binance"
    monkeypatch.setattr(
        engine_main, "discover_active_btc_window", AsyncMock(return_value=_fake_market())
    )
    monkeypatch.setattr(
        engine_main, "fetch_open_price_at_window_start", AsyncMock(return_value=100_000.0)
    )
    with patch.object(
        engine_main.chainlink_stream, "get_price_to_beat"
    ) as fake_cl_ptb:
        r = client.get("/api/market/current")

    assert r.status_code == 200
    body = r.json()
    assert body["price_to_beat"] == 100_000.0
    assert body["price_to_beat_source"] == "binance_1m"
    fake_cl_ptb.assert_not_called()


def test_polymarket_mode_still_prefers_chainlink_stream(client: TestClient, monkeypatch):
    data_source.set_active("polymarket")
    engine_main.runner.rt.config.data_source = "polymarket"
    monkeypatch.setattr(
        engine_main, "discover_active_btc_window", AsyncMock(return_value=_fake_market())
    )
    monkeypatch.setattr(
        engine_main,
        "fetch_chainlink_btc_usd_polygon_at_window_start",
        AsyncMock(return_value=None),
    )
    with patch.object(
        engine_main.chainlink_stream, "get_price_to_beat", return_value=99_950.0
    ) as fake_cl_ptb:
        r = client.get("/api/market/current")

    assert r.status_code == 200
    body = r.json()
    assert body["price_to_beat"] == 99_950.0
    assert body["price_to_beat_source"] == "chainlink_stream"
    fake_cl_ptb.assert_called()
