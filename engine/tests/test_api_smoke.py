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
        "order_mode": "market",
        "market_max_entry_price_cents": 65,
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
    # שדה תקרת מחיר ה-market עושה round-trip מלא (POST -> config -> GET)
    assert j.get("market_max_entry_price_cents") == 65
    assert isinstance(j.get("ui_runtime_started_ts"), (int, float))
    assert isinstance(j.get("ui_runtime_uptime_sec"), (int, float))
    assert isinstance(j.get("ui_runtime_equity_baseline_usd"), (int, float))


def test_strategy_config_clamps_min_contracts_to_market_floor(client: TestClient, monkeypatch):
    """אחרי שמירה — min_contracts לא נשאר מתחת למינימום השוק (מ־discover)."""
    import main as engine_main
    from types import SimpleNamespace

    async def fake_discover(window="5m"):
        return SimpleNamespace(order_min_size=7.0)

    monkeypatch.setattr(engine_main, "discover_active_btc_window", fake_discover)

    body = {
        "investment_usd": 5.0,
        "entry_price_cents": 30,
        "min_contracts": 3,
        "take_profit_pct": 12.0,
        "min_minutes_for_entry": 3,
        "freeze_last_minutes": 1,
        "intermediate_block_new_entries": True,
        "dca_enabled": False,
        "dca_slices": 4,
        "dca_interval_sec": 30,
        "dca_discount_enabled": False,
        "dca_discount_pct": 2,
        "hedge_enabled": False,
        "hedge_combined_ask_max": 0.98,
        "side_preference": "Up",
        "btc_window": "5m",
        "auto_reenter_after_tp": True,
        "reenter_cooldown_sec": 8,
        "max_entries_per_window": 3,
        "max_notional_per_window_usd": 1_000_000,
        "max_trades_per_hour": 1000,
        "near_entry_pct": 3,
        "near_tp_pct": 2,
        "dca_tp_override_pct": 50,
        "book_log_interval_sec": 0,
        "loss_recovery_enabled": False,
        "loss_recovery_step_pct": 20,
        "loss_recovery_every_n_losses": 1,
        "loss_recovery_max_multiplier": 10,
    }
    r = client.post("/api/strategy/config", json=body)
    assert r.status_code == 200
    out = r.json().get("config") or {}
    assert out.get("min_contracts") == 7
    assert engine_main.runner.rt.config.min_contracts == 7
    r2 = client.get("/api/strategy/config")
    assert r2.json().get("min_contracts") == 7


def test_faults_endpoints_roundtrip(client: TestClient, tmp_path, monkeypatch):
    import fault_tracker
    monkeypatch.setattr(fault_tracker, "_DB_PATH", tmp_path / "faults.db")
    monkeypatch.setattr(fault_tracker, "_conn", None)

    r = client.post("/api/faults", json={"title": "test bug", "severity": "high", "detail": "d"})
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.get("/api/faults")
    j = r.json()
    assert len(j["faults"]) == 1
    assert j["counts"]["open"] == 1
    assert j["counts"]["open_severe"] == 1
    fid = j["faults"][0]["id"]

    r = client.post(f"/api/faults/{fid}/handled", json={"handled": True, "resolution_note": "fixed"})
    assert r.json()["ok"] is True
    r = client.get("/api/faults?handled=true")
    rows = r.json()["faults"]
    assert len(rows) == 1 and rows[0]["resolution_note"] == "fixed"

    r = client.request("DELETE", "/api/faults", params={"only_handled": "true"})
    assert r.json()["removed"] == 1


def test_demo_state_uses_fast_serializer(client: TestClient):
    """PR-E: /api/demo/state חייב להחזיר דרך etag_json_response (json.dumps ישיר, לא
    jsonable_encoder האיטי שחנק את ה-event-loop ~3s על 50k עסקאות). הסימן: כותרת ETag קיימת,
    והתוכן זהה (מפתחות הליבה + שדות ה-win-rate שמתווספים ב-handler)."""
    r = client.get("/api/demo/state")
    assert r.status_code == 200
    assert r.headers.get("etag"), "expected ETag header (proves the fast json.dumps path, not jsonable_encoder)"
    j = r.json()
    for k in ("balance_usd", "trades", "equity_history", "positions",
              "bot_run_win_rate_pct", "ui_runtime_equity_baseline_usd"):
        assert k in j, f"missing key {k} after serializer swap"
    assert isinstance(j["trades"], list)


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
    assert isinstance(st.get("stats_epoch_ts"), (int, float))
    assert engine_main.runner.rt.dca_done_slices == 0
    assert engine_main.runner.rt.dca_last_fill_price is None
    assert engine_main.runner.rt.tp_happened_this_window is False

    engine_main.runner.rt.dca_done_slices = 5
    engine_main.demo.state.loss_recovery_streak = 3
    engine_main.demo.state.loss_recovery_multiplier = 2.0
    engine_main.demo.state.trades = [{"id": "keep-me", "ts": 100.0, "type": "BUY", "token_id": "tok"}]
    engine_main.demo.save()
    n_trades_before = len(engine_main.demo.state.trades)
    r = client.post("/api/demo/clear-stats", json={})
    assert r.status_code == 200
    assert engine_main.runner.rt.dca_done_slices == 0
    assert engine_main.demo.state.loss_recovery_streak == 0
    assert engine_main.demo.state.loss_recovery_multiplier == pytest.approx(1.0)
    st2 = client.get("/api/demo/state").json()
    assert len(st2["trades"]) == n_trades_before
    assert isinstance(st2.get("stats_epoch_ts"), (int, float))


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


def test_polymarket_clob_account_without_key(client: TestClient, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    r = client.get("/api/live/polymarket-clob-account")
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is False
    assert "error" in j


# ────────────────── Private key persist / delete ──────────────────


def test_private_key_persist_and_autoload(client: TestClient, monkeypatch):
    """POST persist=true שומר ב-keyring, ובעליה חוזרת המפתח נטען אוטומטית."""
    import os
    import secret_store

    store: dict[str, str] = {}
    monkeypatch.setattr(secret_store, "save_key", lambda k: (store.__setitem__("key", k), True)[1])
    monkeypatch.setattr(secret_store, "load_key", lambda: store.get("key"))
    monkeypatch.setattr(secret_store, "has_persisted_key", lambda: "key" in store)

    r = client.post("/api/live/private-key", json={"key": "0xABC", "persist": True})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["set"] is True
    assert j["persisted"] is True
    assert j["persisted_in_keychain"] is True
    assert store["key"] == "0xABC"
    assert os.environ.get("POLYMARKET_PRIVATE_KEY") == "0xABC"

    # סימולציה של עלייה חדשה: מוחקים את env, טוענים מחדש
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    import main as engine_main
    engine_main._autoload_private_key_from_store()
    assert os.environ.get("POLYMARKET_PRIVATE_KEY") == "0xABC"


def test_private_key_session_only(client: TestClient, monkeypatch):
    """POST persist=false לא נוגע ב-keyring."""
    import secret_store

    store: dict[str, str] = {}
    monkeypatch.setattr(secret_store, "save_key", lambda k: (store.__setitem__("key", k), True)[1])
    monkeypatch.setattr(secret_store, "load_key", lambda: store.get("key"))
    monkeypatch.setattr(secret_store, "has_persisted_key", lambda: "key" in store)

    r = client.post("/api/live/private-key", json={"key": "0xTEMP", "persist": False})
    assert r.status_code == 200
    j = r.json()
    assert j["set"] is True
    assert j["persisted"] is False
    assert "key" not in store  # keyring not touched


def test_private_key_delete(client: TestClient, monkeypatch):
    """DELETE מנקה env + keyring."""
    import os
    import secret_store

    os.environ["POLYMARKET_PRIVATE_KEY"] = "0xDEL"
    deleted_from = {}
    monkeypatch.setattr(secret_store, "delete_key", lambda: (deleted_from.__setitem__("called", True), True)[1])
    monkeypatch.setattr(secret_store, "has_persisted_key", lambda: False)

    r = client.delete("/api/live/private-key")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["removed_from_keychain"] is True
    assert os.environ.get("POLYMARKET_PRIVATE_KEY") == ""
    assert deleted_from.get("called") is True


def test_live_mode_state_includes_persisted(client: TestClient, monkeypatch):
    """GET /api/live/mode includes persisted_in_keychain field."""
    import secret_store
    import main as engine_main
    monkeypatch.setattr(secret_store, "has_persisted_key", lambda: True)
    engine_main._invalidate_persisted_key_cache()

    r = client.get("/api/live/mode")
    assert r.status_code == 200
    j = r.json()
    assert "persisted_in_keychain" in j
    assert j["persisted_in_keychain"] is True


def test_live_portfolio_without_key(client: TestClient):
    """GET /api/live/portfolio returns ok=False when no key."""
    r = client.get("/api/live/portfolio")
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is False


# ────────────────── Trade Audit Ledger endpoints ──────────────────


def test_audit_list_and_export_endpoints(client: TestClient):
    """GET /api/audit -> rows+counts; GET /api/audit/export?labels_only -> rows."""
    r = client.get("/api/audit")
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j.get("rows"), list)
    assert "counts" in j

    r = client.get("/api/audit/export", params={"labels_only": "true"})
    assert r.status_code == 200
    assert isinstance(r.json().get("rows"), list)

    # Trade Coach lessons endpoint must not be shadowed by /api/audit/{session_id}
    r = client.get("/api/audit/lessons")
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j.get("lessons"), list)
    assert "eras" in j and "note" in j

