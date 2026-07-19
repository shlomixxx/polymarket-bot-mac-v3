"""פיצול סטטיסטיקת דמו לפי מנוע (אסטרטגיה מול טריגר/מסחר-מהיר).

היום win-rate + equity מאחדים את שני המנועים יחד, כך שתוצאות הטריגר מתחזות
לביצועי האסטרטגיה. הטסטים כאן מוודאים ש-``by_engine`` נוסף לשני ה-builders,
עם ייחוס נכון של יציאות למנוע שפתח אותן (דרך session_id של ה-BUY), ובלי לשבור
את המפתחות המאוחדים הקיימים שה-UI כבר קורא.
"""
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


# ---------- helper: classify engine source from gate ----------

def test_engine_source_from_gate_classifier():
    import main as m

    assert m._engine_source_from_gate("trigger:signal") == "trigger"
    assert m._engine_source_from_gate("trigger:expire_rollover") == "trigger"
    assert m._engine_source_from_gate("") == "strategy"
    assert m._engine_source_from_gate(None) == "strategy"
    # gate שאינו של הטריגר (למשל gate של האסטרטגיה) -> strategy
    assert m._engine_source_from_gate("entry_ok:Up") == "strategy"


# ---------- win-rate builder: by_engine breakdown ----------

def _seed_two_engine_book(m):
    """BUY של טריגר שיצא ב-SELL_TP (בלי gate משלו!) + BUY של אסטרטגיה שיצא ב-SETTLE_LOSS.

    זהו התרחיש האמיתי: עסקת ה-SELL_TP לא מעתיקה את ה-gate של ה-BUY, לכן ייחוס
    לפי ה-gate של היציאה עצמה היה משקר וסופר אותה כ"אסטרטגיה". הייחוס הנכון הוא
    דרך session_id חזרה ל-BUY הפותח.
    """
    m.demo.reset(10_000.0)
    m._bot_run_started_ts = 1000.0
    m._WIN_RATE_CACHE["key"] = None  # לא להחזיר cache ישן
    m.demo.state.trades = [
        {"ts": 2000.0, "type": "BUY", "session_id": "S_TRIG", "gate": "trigger:signal"},
        {"ts": 2001.0, "type": "SELL_TP", "session_id": "S_TRIG", "realized_pnl": 5.0},
        {"ts": 2002.0, "type": "BUY", "session_id": "S_STRAT"},  # אין gate => אסטרטגיה
        {"ts": 2003.0, "type": "SETTLE_LOSS", "session_id": "S_STRAT", "realized_pnl": -3.0},
    ]


def test_win_rate_by_engine_present_with_correct_breakdown():
    import main as m

    _seed_two_engine_book(m)
    r = m._bot_run_win_rate_stats()

    # --- המפתחות המאוחדים הקיימים חייבים להישאר בדיוק כמו שהיו (backward-safe) ---
    assert r["bot_run_exit_trades_n"] == 2
    assert r["bot_run_wins_n"] == 1
    assert r["bot_run_win_rate_pct"] == 50.0

    # --- by_engine נוסף עם שני המנועים ---
    assert "by_engine" in r
    be = r["by_engine"]
    assert set(be.keys()) == {"strategy", "trigger"}

    trig = be["trigger"]
    strat = be["strategy"]

    # יציאת ה-SELL_TP (ללא gate משלה) יוחסה לטריגר דרך ה-BUY trigger:signal
    assert trig["bot_run_exit_trades_n"] == 1
    assert trig["bot_run_wins_n"] == 1
    assert trig["bot_run_losses_n"] == 0
    assert trig["bot_run_win_rate_pct"] == 100.0
    assert trig["bot_run_realized_pnl_usd"] == 5.0

    # יציאת האסטרטגיה
    assert strat["bot_run_exit_trades_n"] == 1
    assert strat["bot_run_wins_n"] == 0
    assert strat["bot_run_losses_n"] == 1
    assert strat["bot_run_win_rate_pct"] == 0.0
    assert strat["bot_run_realized_pnl_usd"] == -3.0

    # שלמות: פיצול לפי מנוע מסתכם למאוחד
    assert trig["bot_run_exit_trades_n"] + strat["bot_run_exit_trades_n"] == r["bot_run_exit_trades_n"]


def test_win_rate_by_engine_empty_shape_when_no_exits():
    import main as m

    m.demo.reset(10_000.0)
    m._bot_run_started_ts = 1000.0
    m._WIN_RATE_CACHE["key"] = None
    m.demo.state.trades = []

    r = m._bot_run_win_rate_stats()
    # backward-safe: המפתחות המאוחדים עדיין כאן
    assert r["bot_run_exit_trades_n"] == 0
    assert r["bot_run_win_rate_pct"] is None
    # by_engine קיים גם כשאין יציאות — שני המנועים באפסים
    assert "by_engine" in r
    for eng in ("strategy", "trigger"):
        sub = r["by_engine"][eng]
        assert sub["bot_run_exit_trades_n"] == 0
        assert sub["bot_run_wins_n"] == 0
        assert sub["bot_run_losses_n"] == 0
        assert sub["bot_run_win_rate_pct"] is None
        assert sub["bot_run_realized_pnl_usd"] == 0.0


# ---------- snapshot/equity builder: by_engine breakdown ----------

def test_snapshot_includes_by_engine(client: TestClient):
    import main as m

    _seed_two_engine_book(m)
    r = client.get("/api/demo/snapshot")
    assert r.status_code == 200
    payload = r.json()

    # backward-safe: המפתחות המאוחדים שה-UI כבר קורא נשארו
    assert "bot_run_win_rate_pct" in payload
    assert "bot_run_exit_trades_n" in payload
    assert "bot_run_wins_n" in payload
    assert payload["bot_run_exit_trades_n"] == 2

    # by_engine נוסף גם ל-snapshot
    assert "by_engine" in payload
    assert set(payload["by_engine"].keys()) == {"strategy", "trigger"}
    assert payload["by_engine"]["trigger"]["bot_run_exit_trades_n"] == 1
    assert payload["by_engine"]["strategy"]["bot_run_exit_trades_n"] == 1
