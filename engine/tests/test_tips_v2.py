"""בדיקות יחידה ל־generate_tips_v2 (ללא תלות בלוגים אמיתיים)."""

from __future__ import annotations

from typing import Any

import pytest

import tips_v2 as tv
from tips_v2 import RunStats, _compute_expectancy


def _base_cfg(**over: Any) -> dict[str, Any]:
    c: dict[str, Any] = {
        "take_profit_pct": 50.0,
        "dca_tp_override_pct": -150.0,
        "dca_enabled": False,
        "dca_slices": 4,
        "dca_interval_sec": 30.0,
        "dca_discount_enabled": False,
        "dca_discount_pct": 2.0,
        "hedge_enabled": False,
        "hedge_combined_ask_max": 0.98,
        "intermediate_block_new_entries": True,
        "min_minutes_for_entry": 3.0,
        "freeze_last_minutes": 1.0,
        "side_preference": "Up",
        "auto_reenter_after_tp": True,
        "reenter_cooldown_sec": 8.0,
    }
    c.update(over)
    return c


def _make_run(cfg: dict[str, Any], tp_count: int, expire_count: int, tp_sum: float, expire_sum_neg: float) -> RunStats:
    ex = _compute_expectancy(tp_count, tp_sum, expire_count, expire_sum_neg)
    total = tp_count + expire_count
    er = expire_count / total if total else 0.0
    avg_ex = abs(expire_sum_neg) / expire_count if expire_count else 0.0
    return RunStats(
        strategy_config=cfg,
        tp_count=tp_count,
        tp_sum=tp_sum,
        expire_count=expire_count,
        expire_sum_neg=expire_sum_neg,
        expectancy=ex,
        expire_rate=er,
        avg_expire_loss_abs=avg_ex,
        tp_peak_sum=0.0,
        tp_peak_count=0,
        tp_trough_sum=0.0,
        tp_trough_count=0,
        expire_trough_sum=0.0,
        expire_trough_count=0,
        after_tp_peak_delta_cents_sum=0.0,
        after_tp_peak_delta_cents_count=0,
        after_tp_peak_delta_pct_sum=0.0,
        after_tp_peak_delta_pct_count=0,
        after_tp_trough_delta_cents_sum=0.0,
        after_tp_trough_delta_cents_count=0,
        after_tp_trough_delta_pct_sum=0.0,
        after_tp_trough_delta_pct_count=0,
        entries_count_sum=0.0,
        entries_count_sessions=0,
        duration_sec_sum=0.0,
        duration_sec_sessions=0,
        btc_window="5m",
        session_outcomes=(),
    )


def test_take_profit_no_contrast_when_same_tp(monkeypatch: pytest.MonkeyPatch) -> None:
    """שתי ריצות עם אותו TP (אחרי עיגוב) → קבוצה אחת → tip_mode no_contrast."""

    def fake_analyze(max_runs: int) -> list[RunStats]:
        c = _base_cfg(take_profit_pct=50.0)
        return [
            _make_run(c, 30, 30, 300.0, -600.0),
            _make_run(c, 30, 30, 300.0, -600.0),
        ]

    monkeypatch.setattr(tv, "analyze_runs", fake_analyze)
    out = tv.generate_tips_v2(max_runs=50, min_samples=50, use_guardrails=False, current_cfg=_base_cfg())
    tip = next((t for t in out["tips"] if t["key"] == "take_profit_pct"), None)
    assert tip is not None
    assert tip.get("tip_mode") == "no_contrast"
    assert tip.get("metrics") is None


def test_take_profit_full_when_two_tp_bins(monkeypatch: pytest.MonkeyPatch) -> None:
    """שתי ריצות עם TP שונה מספיק → שתי קבוצות → tip_mode full + bin_comparison."""

    def fake_analyze(max_runs: int) -> list[RunStats]:
        return [
            _make_run(_base_cfg(take_profit_pct=50.0), 30, 30, 300.0, -600.0),
            _make_run(_base_cfg(take_profit_pct=20.0), 30, 30, 400.0, -400.0),
        ]

    monkeypatch.setattr(tv, "analyze_runs", fake_analyze)
    out = tv.generate_tips_v2(max_runs=50, min_samples=50, use_guardrails=False, current_cfg=_base_cfg(take_profit_pct=50.0))
    tip = next((t for t in out["tips"] if t["key"] == "take_profit_pct"), None)
    assert tip is not None
    assert tip.get("tip_mode") == "full"
    assert tip.get("metrics") is not None
    bc = tip.get("bin_comparison") or []
    assert len(bc) >= 2


def test_global_metrics_and_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_analyze(max_runs: int) -> list[RunStats]:
        c = _base_cfg()
        return [_make_run(c, 25, 25, 100.0, -200.0)]

    monkeypatch.setattr(tv, "analyze_runs", fake_analyze)
    out = tv.generate_tips_v2(max_runs=50, min_samples=10, use_guardrails=False, current_cfg=_base_cfg())
    assert out.get("global_metrics") is not None
    assert out.get("global_narrative")
    assert isinstance(out.get("data_quality"), dict)
    dq = out["data_quality"]
    assert dq["runs_used"] == 1
    assert "summary" in out and len(out["summary"]) > 10
    assert "window_comparison" in out and "5m" in out["window_comparison"]
    w5 = out["by_btc_window"]["5m"]
    assert "extended_metrics" in w5
    assert w5["extended_metrics"] is not None
    assert "profit_factor" in w5["extended_metrics"]
    assert "by_side" in w5["extended_metrics"]
