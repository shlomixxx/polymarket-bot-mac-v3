import sys
from pathlib import Path

import pytest


ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))


@pytest.fixture(autouse=True)
def _isolate_fault_tracker_db(tmp_path, monkeypatch):
    """בידוד גורף: כל טסט כותב תקלות ל-faults.db זמני משלו — לעולם לא נוגע ב-engine/faults.db
    החי שאותו מגיש המנוע הרץ (לשונית "תקלות"). מאפס את ה-conn המטמון לפני ואחרי כל טסט כדי
    שהנתיב הזמני ייכנס לתוקף ולא ידלוף בין טסטים. יבוא עצל כדי לא לתלות את איסוף-הבדיקות."""
    import fault_tracker

    monkeypatch.setattr(fault_tracker, "_DB_PATH", tmp_path / "faults.db", raising=False)
    fault_tracker.reset()
    try:
        yield
    finally:
        fault_tracker.reset()

