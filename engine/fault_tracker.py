"""
מסד נתוני תקלות/באגים של המערכת (SQLite) — לשונית "תקלות" ב-UI ולמעקב.

עיצוב:
- לעולם לא זורק חריגה אל תוך לולאת המסחר (כל פעולה עטופה try/except ומחזירה bool/ברירת־מחדל).
- dedup לפי dedup_key: תקלה חוזרת לא יוצרת אלפי שורות — מגדילה count ומעדכנת last_ts.
- נשמר ב-DATA_ROOT/faults.db כדי לשרוד restart של Railway (כמו history.db).
- חומרה: critical | high | medium | low. קטגוריה חופשית (risk/settlement/entry_failed/...).
- handled: האם טופל. resolved_ts + resolution_note לתיעוד.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_DB_PATH = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))) / "faults.db"
_conn: Optional[sqlite3.Connection] = None
# מנעול לחיבור ה-sqlite המשותף (check_same_thread=False). היום כל הכותבים על אותו event-loop
# thread, אבל ה-watchdog וכותבים עתידיים עלולים לבוא מ-thread אחר — המנעול מונע מרוץ כתיבה.
_LOCK = threading.Lock()

SEVERITIES = ("critical", "high", "medium", "low")


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=5.0)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout=5000")  # אין WAL — חסר תועלת ב-thread יחיד + סיכון על volume רשת
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS faults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT UNIQUE,
                first_ts REAL NOT NULL,
                last_ts REAL NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                category TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT,
                source TEXT,
                context_json TEXT,
                handled INTEGER NOT NULL DEFAULT 0,
                resolved_ts REAL,
                resolution_note TEXT
            )
            """
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_faults_last_ts ON faults(last_ts)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_faults_handled ON faults(handled)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_faults_severity ON faults(severity)")
        _conn.commit()
    return _conn


def record_fault(
    *,
    category: str,
    severity: str,
    title: str,
    detail: str = "",
    source: str = "",
    context: Optional[dict[str, Any]] = None,
    dedup_key: Optional[str] = None,
    reopen_on_recur: bool = True,
) -> bool:
    """רושם תקלה (או מגדיל count אם dedup_key קיים). לעולם לא זורק.

    reopen_on_recur=True: אם תקלה שטופלה חוזרת — נפתחת מחדש (handled=0) כדי לא לפספס
    הישנות. אם אתה רוצה שתישאר "טופל" גם בהישנות — העבר False.
    """
    try:
        sev = str(severity).lower().strip()
        if sev not in SEVERITIES:
            sev = "medium"
        now = time.time()
        key = dedup_key or f"{category}:{title}"
        ctx = json.dumps(context or {}, ensure_ascii=False)
        reopen_sql = "handled = 0, resolved_ts = NULL, resolution_note = NULL," if reopen_on_recur else ""
        with _LOCK:
            conn = _get_conn()
            conn.execute(
                f"""
                INSERT INTO faults (dedup_key, first_ts, last_ts, count, category,
                                    severity, title, detail, source, context_json)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET
                    last_ts  = excluded.last_ts,
                    count    = count + 1,
                    {reopen_sql}
                    detail   = excluded.detail,
                    severity = excluded.severity,
                    context_json = excluded.context_json
                """,
                (key, now, now, str(category), sev, str(title), str(detail), str(source), ctx),
            )
            conn.commit()
        return True
    except Exception as e:  # אסור להפיל את לולאת המסחר בגלל לוג תקלה
        print(f"[fault_tracker] record_fault failed: {e!r}", flush=True)
        return False


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    raw = d.pop("context_json", None)
    try:
        d["context"] = json.loads(raw) if raw else {}
    except Exception:
        d["context"] = {}
    d["handled"] = bool(d.get("handled"))
    return d


def list_faults(
    *,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    handled: Optional[bool] = None,
    since_ts: Optional[float] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """מחזיר תקלות לפי מסננים. מסודר: לא-טופל קודם, אז last_ts יורד."""
    try:
        conn = _get_conn()
        where: list[str] = []
        args: list[Any] = []
        if category:
            where.append("category = ?")
            args.append(category)
        if severity:
            where.append("severity = ?")
            args.append(severity)
        if handled is not None:
            where.append("handled = ?")
            args.append(1 if handled else 0)
        if since_ts is not None:
            where.append("last_ts >= ?")
            args.append(float(since_ts))
        sql = "SELECT * FROM faults"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # critical>high>medium>low ע"י CASE; אז לא-טופל קודם; אז עדכני קודם
        sql += (
            " ORDER BY handled ASC, "
            " CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            " WHEN 'medium' THEN 2 ELSE 3 END ASC, last_ts DESC LIMIT ?"
        )
        args.append(max(1, min(int(limit), 5000)))
        rows = conn.execute(sql, args).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[fault_tracker] list_faults failed: {e!r}", flush=True)
        return []


def fault_counts() -> dict[str, Any]:
    """ספירות מצרפיות ללשונית: לפי חומרה, וכמה לא-טופלו."""
    try:
        conn = _get_conn()
        out: dict[str, Any] = {"by_severity": {}, "open": 0, "handled": 0, "total": 0}
        for r in conn.execute("SELECT severity, COUNT(*) c FROM faults GROUP BY severity"):
            out["by_severity"][r["severity"]] = r["c"]
        row = conn.execute(
            "SELECT COUNT(*) total, SUM(CASE WHEN handled=0 THEN 1 ELSE 0 END) open_n FROM faults"
        ).fetchone()
        out["total"] = int(row["total"] or 0)
        out["open"] = int(row["open_n"] or 0)
        out["handled"] = out["total"] - out["open"]
        # כמה תקלות פתוחות חמורות (critical/high) — לדגל אדום ב-UI
        sev_open = conn.execute(
            "SELECT COUNT(*) c FROM faults WHERE handled=0 AND severity IN ('critical','high')"
        ).fetchone()
        out["open_severe"] = int(sev_open["c"] or 0)
        return out
    except Exception as e:
        print(f"[fault_tracker] fault_counts failed: {e!r}", flush=True)
        return {"by_severity": {}, "open": 0, "handled": 0, "total": 0, "open_severe": 0}


def mark_handled(fault_id: int, handled: bool = True, resolution_note: str = "") -> bool:
    try:
        with _LOCK:
            conn = _get_conn()
            conn.execute(
                "UPDATE faults SET handled=?, resolved_ts=?, resolution_note=? WHERE id=?",
                (1 if handled else 0, time.time() if handled else None,
                 str(resolution_note) if handled else None, int(fault_id)),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[fault_tracker] mark_handled failed: {e!r}", flush=True)
        return False


def add_manual(
    *, title: str, detail: str = "", severity: str = "medium", category: str = "manual"
) -> bool:
    """תקלה שדווחה ידנית ע"י המשתמש (כל אחת ייחודית — ts במפתח)."""
    return record_fault(
        category=category, severity=severity, title=title, detail=detail,
        source="manual", dedup_key=f"manual:{title}:{time.time()}",
    )


def clear_faults(only_handled: bool = True) -> int:
    """מוחק תקלות. אם only_handled — רק את אלה שטופלו. מחזיר כמה נמחקו."""
    try:
        with _LOCK:
            conn = _get_conn()
            cur = conn.execute("DELETE FROM faults WHERE handled=1" if only_handled else "DELETE FROM faults")
            conn.commit()
            return int(cur.rowcount or 0)
    except Exception as e:
        print(f"[fault_tracker] clear_faults failed: {e!r}", flush=True)
        return 0
