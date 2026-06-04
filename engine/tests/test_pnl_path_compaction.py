"""PR-G: pnl_path על עסקאות שנסגרו היה לא-חסום (~600 נק׳/עסקה ⇒ state 26MB ⇒ json.dumps ~2s
שחנק את ה-event-loop, תקלת event_loop_lag). הפתרון: לקצץ את pnl_path של עסקה היסטורית
ל-SETTLED_PNL_PATH_MAX נקודות (משמר כניסה/peak/trough/אחרונות), גם בסגירה וגם דחיסה חד-פעמית
של ה-backlog הקיים בעת טעינת ה-state."""
import json
from pathlib import Path

import demo_engine
from demo_engine import DemoEngine, _trim_settled_path, SETTLED_PNL_PATH_MAX


def _fake_path(n: int) -> list:
    return [{"ts": 1000.0 + i, "upnl_pct": float(i), "bid": 0.5, "balance": 100.0 + i, "equity": 100.0 + i} for i in range(n)]


def test_trim_settled_path_caps_and_preserves_endpoints():
    path = _fake_path(600)
    tr = {"path": path, "high_watermark_ts": 1000.0 + 123, "low_watermark_ts": 1000.0 + 456}
    out = _trim_settled_path(tr)
    assert len(out) <= SETTLED_PNL_PATH_MAX, "settled path must be capped"
    assert len(out) < len(path), "a 600-point path must actually shrink"
    # entry + last preserved (chart endpoints)
    assert out[0]["ts"] == path[0]["ts"]
    assert out[-1]["ts"] == path[-1]["ts"]
    # peak + trough samples preserved (chart extremes stay visible)
    kept_ts = {p["ts"] for p in out}
    assert (1000.0 + 123) in kept_ts, "peak sample must survive the trim"
    assert (1000.0 + 456) in kept_ts, "trough sample must survive the trim"


def test_trim_settled_path_keeps_small_paths_intact():
    path = _fake_path(10)
    out = _trim_settled_path({"path": path})
    assert out == path  # nothing to trim


def test_trim_settled_path_handles_missing_path():
    assert _trim_settled_path({}) == []
    assert _trim_settled_path({"path": None}) == []


def test_load_compacts_existing_oversized_pnl_paths(tmp_path: Path):
    """ה-backlog הקיים (עסקאות עם pnl_path ענק) נדחס בטעינה — אחרת ה-26MB נשאר עד שכל
    עסקה תיכתב מחדש (לעולם, כי עסקאות שנסגרו אינן משתנות)."""
    big = _fake_path(600)
    small = _fake_path(20)
    state = {
        "balance_usd": 1000.0,
        "positions": [],
        "trades": [
            {"id": "a", "ts": 1.0, "type": "SETTLE_LOSS", "token_id": "t1", "pnl_path": big},
            {"id": "b", "ts": 2.0, "type": "SELL_TP", "token_id": "t2", "pnl_path": small},
            {"id": "c", "ts": 3.0, "type": "BUY", "token_id": "t3"},  # no pnl_path
        ],
        "equity_history": [],
        "trade_seq": 3,
    }
    sp = tmp_path / "demo_state.json"
    sp.write_text(json.dumps(state))
    eng = DemoEngine(state_path=sp)
    trades = {t["id"]: t for t in eng.state.trades}
    assert len(trades["a"]["pnl_path"]) <= SETTLED_PNL_PATH_MAX, "oversized path must be compacted on load"
    assert trades["b"]["pnl_path"] == small, "already-small path is untouched"
    assert "pnl_path" not in trades["c"] or not trades["c"].get("pnl_path")
    # endpoints of the compacted path are preserved
    assert trades["a"]["pnl_path"][0]["ts"] == big[0]["ts"]
    assert trades["a"]["pnl_path"][-1]["ts"] == big[-1]["ts"]


def test_settled_path_cap_is_sane_default():
    assert 20 <= SETTLED_PNL_PATH_MAX <= 200
