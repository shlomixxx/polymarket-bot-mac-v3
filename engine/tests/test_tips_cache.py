"""טסטים ל-B-14: cache ל-analyze_runs לפי מצב תיקיות הריצה (לא לפי עסקאות דמו)."""
import tips_v2


def _make_run_dir(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    (d / "strategy_snapshot.json").write_text("{}")
    (d / "trades.json").write_text("{}")
    return d


def test_analyze_runs_cached_by_dir_state(tmp_path, monkeypatch):
    d1 = _make_run_dir(tmp_path, "run1")
    monkeypatch.setattr(tips_v2, "list_run_dirs", lambda mr: [d1])
    loads = {"n": 0}

    def counting_load(p):
        loads["n"] += 1
        return {"strategy_config": {}, "trades": []}

    monkeypatch.setattr(tips_v2, "_load_json", counting_load)
    monkeypatch.setattr(tips_v2, "_analyze_trades", lambda trades, strategy_config: object())
    tips_v2._ANALYZE_RUNS_CACHE["key"] = None
    tips_v2._ANALYZE_RUNS_CACHE["val"] = None

    r1 = tips_v2.analyze_runs(50)
    after_first = loads["n"]
    r2 = tips_v2.analyze_runs(50)
    assert loads["n"] == after_first  # קריאה שנייה מ-cache — אין re-parse
    assert len(r1) == 1 and len(r2) == 1


def test_analyze_runs_returns_private_list(tmp_path, monkeypatch):
    """generate_tips_v2 ממזג עסקאות חיות ע"י mutation של הרשימה — אסור שתשבור את ה-cache."""
    d1 = _make_run_dir(tmp_path, "run1")
    monkeypatch.setattr(tips_v2, "list_run_dirs", lambda mr: [d1])
    monkeypatch.setattr(tips_v2, "_load_json", lambda p: {"strategy_config": {}, "trades": []})
    monkeypatch.setattr(tips_v2, "_analyze_trades", lambda trades, strategy_config: object())
    tips_v2._ANALYZE_RUNS_CACHE["key"] = None
    tips_v2._ANALYZE_RUNS_CACHE["val"] = None

    r1 = tips_v2.analyze_runs(50)
    r1.insert(0, "SENTINEL")  # מדמה את המיזוג ב-generate_tips_v2
    r2 = tips_v2.analyze_runs(50)
    assert "SENTINEL" not in r2  # ה-cache לא נפגע
