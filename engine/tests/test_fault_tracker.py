"""טסטים ל-fault_tracker (מסד תקלות + dedup + מצב טופל)."""
from __future__ import annotations

from pathlib import Path

import pytest

import fault_tracker


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path: Path, monkeypatch):
    """כל טסט עם DB נקי משלו."""
    monkeypatch.setattr(fault_tracker, "_DB_PATH", tmp_path / "faults.db")
    monkeypatch.setattr(fault_tracker, "_conn", None)
    yield


def test_record_and_list():
    assert fault_tracker.record_fault(
        category="settlement", severity="high", title="x", detail="d", dedup_key="k1"
    )
    rows = fault_tracker.list_faults()
    assert len(rows) == 1
    assert rows[0]["title"] == "x"
    assert rows[0]["count"] == 1
    assert rows[0]["handled"] is False


def test_dedup_increments_count_not_rows():
    for _ in range(5):
        fault_tracker.record_fault(category="entry_failed", severity="medium", title="y", dedup_key="dup")
    rows = fault_tracker.list_faults()
    assert len(rows) == 1
    assert rows[0]["count"] == 5


def test_counts_aggregate_and_open_severe():
    fault_tracker.record_fault(category="a", severity="critical", title="c", dedup_key="c")
    fault_tracker.record_fault(category="a", severity="low", title="l", dedup_key="l")
    counts = fault_tracker.fault_counts()
    assert counts["total"] == 2
    assert counts["open"] == 2
    assert counts["open_severe"] == 1  # רק ה-critical
    assert counts["by_severity"].get("critical") == 1


def test_mark_handled_then_reopen_on_recur():
    fault_tracker.record_fault(category="a", severity="high", title="t", dedup_key="r")
    rid = fault_tracker.list_faults()[0]["id"]
    assert fault_tracker.mark_handled(rid, True, "fixed")
    assert fault_tracker.list_faults(handled=True)[0]["resolution_note"] == "fixed"
    # הישנות פותחת מחדש (reopen_on_recur ברירת מחדל)
    fault_tracker.record_fault(category="a", severity="high", title="t", dedup_key="r")
    row = fault_tracker.list_faults()[0]
    assert row["handled"] is False
    assert row["count"] == 2


def test_severity_ordering_open_first():
    fault_tracker.record_fault(category="a", severity="low", title="low", dedup_key="lo")
    fault_tracker.record_fault(category="a", severity="critical", title="crit", dedup_key="cr")
    fault_tracker.mark_handled(fault_tracker.list_faults(severity="critical")[0]["id"], True)
    rows = fault_tracker.list_faults()
    # לא-טופל קודם → ה-low הפתוח לפני ה-critical שטופל
    assert rows[0]["title"] == "low"


def test_clear_only_handled():
    fault_tracker.record_fault(category="a", severity="high", title="keep", dedup_key="k")
    fault_tracker.record_fault(category="a", severity="high", title="gone", dedup_key="g")
    fault_tracker.mark_handled(fault_tracker.list_faults(severity="high", handled=False)[1]["id"], True)
    removed = fault_tracker.clear_faults(only_handled=True)
    assert removed == 1
    assert len(fault_tracker.list_faults()) == 1


def test_record_fault_never_raises_on_bad_input():
    # severity לא חוקי → נשמר כ-medium, לא זורק
    assert fault_tracker.record_fault(category="a", severity="???", title="t", dedup_key="bad")
    assert fault_tracker.list_faults()[0]["severity"] == "medium"
