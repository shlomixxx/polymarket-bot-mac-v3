from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    # Import inside fixture so we can isolate global state per test run
    import main as engine_main

    # Redirect demo state to temp file to avoid touching real demo_state.json
    engine_main.demo.state_path = tmp_path / "demo_state.json"
    engine_main.demo.reset(10_000.0)
    engine_main.runner.rt.mode = "off"

    return TestClient(engine_main.app)


def test_health_head_and_get(client: TestClient):
    r = client.head("/api/health")
    assert r.status_code == 200
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_logs_run_dir_without_env(client: TestClient):
    r = client.get("/api/logs/run-dir")
    assert r.status_code == 200
    j = r.json()
    assert j.get("active") is False
    assert j.get("log_run_dir") in (None, "")


def test_strategy_config_roundtrip(client: TestClient):
    body = {
        "investment_usd": 7.5,
        "entry_price_cents": 30,
        "min_contracts": 5,
        "take_profit_pct": 12.5,
        "min_minutes_for_entry": 3,
        "freeze_last_minutes": 1,
        "intermediate_block_new_entries": True,
        "dca_enabled": False,
        "dca_slices": 4,
        "dca_interval_sec": 30,
        "hedge_enabled": False,
        "hedge_combined_ask_max": 0.98,
        "side_preference": "Up",
        "btc_window": "15m",
    }
    r = client.post("/api/strategy/config", json=body)
    assert r.status_code == 200
    r = client.get("/api/strategy/config")
    assert r.status_code == 200
    j = r.json()
    assert j["investment_usd"] == 7.5
    assert j["entry_price_cents"] == 30
    assert j["take_profit_pct"] == 12.5
    assert j.get("btc_window") == "15m"


def test_demo_reset_and_clear_stats(client: TestClient):
    import main as engine_main

    # מצב רפאים: DCA נשאר בזיכרון אחרי איפוס דמו — חייב להתאפס יחד עם הפוזיציות
    engine_main.runner.rt.dca_done_slices = 2
    engine_main.runner.rt.dca_last_fill_price = 0.42
    engine_main.runner.rt.tp_happened_this_window = True

    r = client.post("/api/demo/reset", json={"balance": 123})
    assert r.status_code == 200
    st = client.get("/api/demo/state").json()
    assert float(st["balance_usd"]) == 123.0
    assert engine_main.runner.rt.dca_done_slices == 0
    assert engine_main.runner.rt.dca_last_fill_price is None
    assert engine_main.runner.rt.tp_happened_this_window is False

    engine_main.runner.rt.dca_done_slices = 5
    r = client.post("/api/demo/clear-stats", json={})
    assert r.status_code == 200
    assert engine_main.runner.rt.dca_done_slices == 0
    st2 = client.get("/api/demo/state").json()
    assert st2["trades"] == []


def test_tips_v2_endpoint_shape(client: TestClient):
    r = client.get("/api/strategy/tips-v2")
    assert r.status_code == 200
    j = r.json()
    assert "generated_at" in j
    assert "summary" in j
    assert "tips" in j and isinstance(j["tips"], list)
    assert "global_metrics" in j
    assert "global_narrative" in j
    assert "data_quality" in j
    assert "by_btc_window" in j
    assert "5m" in j["by_btc_window"] and "15m" in j["by_btc_window"]
    assert "window_comparison" in j
    assert "5m" in j["window_comparison"] and "15m" in j["window_comparison"]


def test_strategy_logs_cleared_on_off_to_auto(client: TestClient):
    import main as engine_main

    engine_main.runner.rt.log_lines = ["old log line"]
    r = client.post("/api/strategy/mode", json={"mode": "auto"})
    assert r.status_code == 200
    lines = client.get("/api/strategy/logs").json().get("lines") or []
    assert lines == []

