"""טסטים לאופטימיזציות המשאבים בצד השרת (Phase 3): A-3, B-8, B-13."""
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


# ---------- A-3: win-rate memoization ----------

def test_win_rate_memoized_when_trades_unchanged():
    """אותו (bot_run_started_ts, len(trades)) -> אותו אובייקט (חישוב פעם אחת, לא O(N) בכל קריאה)."""
    import main as m
    m.demo.reset(10_000.0)
    m._bot_run_started_ts = 1000.0
    m.demo.state.trades = [{"ts": 2000.0, "type": "SETTLE_WIN", "realized_pnl": 5.0}]

    r1 = m._bot_run_win_rate_stats()
    r2 = m._bot_run_win_rate_stats()
    assert r1 is r2  # ממומואיז — אותו אובייקט בדיוק
    assert r1["bot_run_wins_n"] == 1
    assert r1["bot_run_exit_trades_n"] == 1


def test_win_rate_recomputes_when_trade_appended():
    """עסקה חדשה (len משתנה) -> חישוב מחדש מיידי, ללא staleness."""
    import main as m
    m.demo.reset(10_000.0)
    m._bot_run_started_ts = 1000.0
    m.demo.state.trades = [{"ts": 2000.0, "type": "SETTLE_WIN", "realized_pnl": 5.0}]

    r1 = m._bot_run_win_rate_stats()
    m.demo.state.trades.append({"ts": 2001.0, "type": "SETTLE_LOSS", "realized_pnl": -5.0})
    r2 = m._bot_run_win_rate_stats()

    assert r2 is not r1
    assert r2["bot_run_exit_trades_n"] == 2
    assert r2["bot_run_win_rate_pct"] == 50.0


# ---------- A-3: snapshot ETag / 304 ----------

def test_snapshot_returns_etag(client: TestClient):
    r = client.get("/api/demo/snapshot")
    assert r.status_code == 200
    assert r.headers.get("etag")


def test_snapshot_304_when_unchanged(client: TestClient):
    r1 = client.get("/api/demo/snapshot")
    etag = r1.headers["etag"]
    r2 = client.get("/api/demo/snapshot", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""


# ---------- B-13: last-window-outcome epoch cache ----------

def test_last_window_outcome_has_etag(client: TestClient):
    r = client.get("/api/history/last-window-outcome")
    assert r.status_code == 200
    assert r.headers.get("etag")


def test_last_window_outcome_304_when_unchanged(client: TestClient):
    r1 = client.get("/api/history/last-window-outcome")
    etag = r1.headers["etag"]
    r2 = client.get("/api/history/last-window-outcome", headers={"If-None-Match": etag})
    assert r2.status_code == 304


# ---------- C-3 / C-4: ETag on live/mode + logs ----------

def test_live_mode_304_when_unchanged(client: TestClient):
    r1 = client.get("/api/live/mode")
    assert r1.status_code == 200
    etag = r1.headers["etag"]
    r2 = client.get("/api/live/mode", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_logs_304_when_unchanged(client: TestClient):
    r1 = client.get("/api/strategy/logs")
    etag = r1.headers["etag"]
    r2 = client.get("/api/strategy/logs", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_log_entries_has_etag(client: TestClient):
    r = client.get("/api/strategy/log-entries")
    assert r.status_code == 200
    assert r.headers.get("etag")


# ---------- B-1: clob-account display cache ----------

def test_clob_account_display_cached(client: TestClient, monkeypatch):
    """שתי קריאות בתוך ה-TTL -> fetch_polymarket_clob_account נקרא פעם אחת (לא auth+balance כפול)."""
    import main as m
    calls = {"n": 0}

    def fake_acct():
        calls["n"] += 1
        return {"ok": True, "balance_usd": 100.0, "address": "0xA"}

    monkeypatch.setattr(m, "fetch_polymarket_clob_account", fake_acct)
    m._CLOB_ACCOUNT_CACHE.invalidate()
    r1 = client.get("/api/live/polymarket-clob-account")
    r2 = client.get("/api/live/polymarket-clob-account")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["balance_usd"] == 100.0
    assert calls["n"] == 1  # השנייה מ-cache


# ---------- B-2: /api/signals handler cache ----------

def test_signals_cached_within_ttl(client: TestClient, monkeypatch):
    """שתי קריאות תוך ה-TTL -> compute_signals + משיכת ספרים פעם אחת."""
    import main as m

    async def fake_discover(w):
        return None  # בלי רשת — לא מושך ספרים

    calls = {"n": 0}

    async def fake_compute(**kw):
        calls["n"] += 1
        return {"recommendation": "Up", "confidence": 0.5}

    monkeypatch.setattr(m, "discover_active_btc_window", fake_discover)
    monkeypatch.setattr(m, "compute_signals", fake_compute)
    m._SIGNALS_CACHE.invalidate()

    r1 = client.get("/api/signals")
    r2 = client.get("/api/signals")
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 1  # השנייה מ-cache


def test_signals_refresh_bypasses_cache(client: TestClient, monkeypatch):
    import main as m

    async def fake_discover(w):
        return None

    calls = {"n": 0}

    async def fake_compute(**kw):
        calls["n"] += 1
        return {"recommendation": "Up"}

    monkeypatch.setattr(m, "discover_active_btc_window", fake_discover)
    monkeypatch.setattr(m, "compute_signals", fake_compute)
    m._SIGNALS_CACHE.invalidate()

    client.get("/api/signals")
    client.get("/api/signals?refresh=true")
    assert calls["n"] == 2  # refresh עוקף את ה-cache
