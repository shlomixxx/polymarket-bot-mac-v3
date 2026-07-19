"""בידוד ה-DB של fault_tracker — ריצות pytest אסור שיזהמו את engine/faults.db החי.

הבאג: fault_tracker._get_conn() קיבע את נתיב ה-DB ב-import (:21) ושמר את ה-conn
במטמון גלובלי כנגד אותו נתיב (:30-33), כך ש-override מאוחר של DATA_ROOT (או של _DB_PATH)
פשוט נבלע — כתיבות דלפו ל-DB הראשון. בבדיקות זה אומר שהן כתבו שורות אמת אל engine/faults.db
שאותו מגיש המנוע החי (לשונית "תקלות").

התיקון: _get_conn פותר את הנתיב בזמן-קריאה ומשחרר conn מיושן; reset() מנקה את המטמון.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def _dedup_keys(db_path: Path) -> set[str]:
    """קורא read-only את dedup_key-ים מ-DB תקלות (בלי לגעת/לשנות שורות)."""
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT dedup_key FROM faults")}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def test_get_conn_reresolves_after_db_path_change(tmp_path, monkeypatch):
    """שחזור הבאג: ה-conn נשמר במטמון כנגד הנתיב הראשון ולא נפתח-מחדש כשהנתיב משתנה,
    כך שכתיבות דולפות ל-DB הישן (בפרוד: engine/faults.db החי). אחרי התיקון _get_conn
    פותר את הנתיב בזמן-קריאה ומשחרר conn מיושן."""
    import fault_tracker

    dir_a = tmp_path / "a"; dir_a.mkdir()
    dir_b = tmp_path / "b"; dir_b.mkdir()
    path_a = dir_a / "faults.db"
    path_b = dir_b / "faults.db"

    # פותח וממטמן את ה-conn כנגד path_a
    monkeypatch.setattr(fault_tracker, "_DB_PATH", path_a)
    monkeypatch.setattr(fault_tracker, "_conn", None)
    assert fault_tracker.record_fault(category="t", severity="low", title="a", dedup_key="ka")

    # מחליף את הנתיב בלי לנקות ידנית את ה-conn המטמון
    monkeypatch.setattr(fault_tracker, "_DB_PATH", path_b)
    assert fault_tracker.record_fault(category="t", severity="low", title="b", dedup_key="kb")

    # 'kb' חייב לנחות ב-DB החדש (פתירת נתיב בזמן-קריאה), לא ב-conn המיושן
    assert _dedup_keys(path_b) == {"kb"}, "write leaked to the STALE cached DB (path not re-resolved)"
    assert _dedup_keys(path_a) == {"ka"}


def test_data_root_override_at_call_time_writes_to_override_not_engine(tmp_path, monkeypatch):
    """item (d): כש-DATA_ROOT נדרס בזמן-ריצה, fault_tracker כותב ל-override ולא ל-engine/faults.db."""
    import fault_tracker

    # אין override מפורש של _DB_PATH → הנתיב חייב להיפתר מ-DATA_ROOT בזמן-קריאה
    monkeypatch.setattr(fault_tracker, "_DB_PATH", None, raising=False)
    data_root = tmp_path / "runtime"; data_root.mkdir()
    monkeypatch.setenv("DATA_ROOT", str(data_root))
    fault_tracker.reset()  # משחרר conn מטמון כדי שה-override ייכנס לתוקף

    key = f"dr-{time.time_ns()}"
    assert fault_tracker.record_fault(category="test", severity="low", title="t", dedup_key=key)

    override_db = data_root / "faults.db"
    assert override_db.exists(), "fault write did not land in the DATA_ROOT override path"
    assert key in _dedup_keys(override_db)

    # ה-DB החי של המנוע לא נגע — המפתח הזמני לא נכתב אליו
    engine_db = Path(fault_tracker.__file__).resolve().parent / "faults.db"
    assert key not in _dedup_keys(engine_db), "test fault polluted the live engine/faults.db"

    fault_tracker.reset()
