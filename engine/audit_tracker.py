"""Trade Audit & Learning Ledger store (SQLite). Twin of fault_tracker.py.

One row per trade-session. Decision-time columns are written ONCE by open_row and
NEVER updated; finalize_row appends settlement-time + derived columns only. Never
raises into the trading loop.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import audit_derive

_DB_PATH = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))) / "audit.db"
_conn: Optional[sqlite3.Connection] = None
_LOCK = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=5.0)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout=5000")
        # synchronous=NORMAL cuts the per-commit fsync cost (open_row/finalize_row commit
        # inline from the async loop at BUY/settlement). We deliberately do NOT enable WAL —
        # like faults.db/history.db, this DB lives on a Railway network volume where WAL is
        # risky and brings no benefit for a single writer.
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_rows (
                session_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL DEFAULT 1,
                code_version TEXT,
                mode TEXT, slug TEXT, epoch INTEGER, window_sec INTEGER, side TEXT,
                decision_ts INTEGER,
                seconds_remaining_at_entry INTEGER, entry_minute_in_window INTEGER,
                recommendation TEXT, weighted_score REAL, confidence_pct REAL,
                vol_bucket TEXT, btc_spot_at_entry REAL, avg_fill_price REAL, contracts REAL,
                investment_usd_effective REAL, loss_recovery_multiplier REAL,
                action_propensity REAL DEFAULT 1.0, exploration_flag INTEGER DEFAULT 0,
                context_json TEXT,
                settled_ts INTEGER, exit_type TEXT, settlement_status TEXT DEFAULT 'PENDING',
                realized_pnl REAL, realized_pct REAL,
                peak_unrealized_pct REAL, trough_unrealized_pct REAL,
                hold_duration_sec REAL, fees REAL,
                settlement_btc_start REAL, settlement_btc_end REAL, resolved_outcome TEXT,
                exit_efficiency REAL, missed_profit_pct REAL, signal_was_correct INTEGER,
                signals_agreement REAL, signal_conflict INTEGER, cf_other_side_pnl REAL,
                dipped_then_won INTEGER, lesson_tag TEXT, rule_flags_json TEXT,
                cf_exit_variants_json TEXT, overlap_group_id TEXT, pnl_path_json TEXT
            )
            """
        )
        _conn.execute("CREATE TABLE IF NOT EXISTS audit_meta (k TEXT PRIMARY KEY, v TEXT)")
        for col in ("decision_ts", "settlement_status", "side", "lesson_tag", "recommendation"):
            _conn.execute(f"CREATE INDEX IF NOT EXISTS idx_audit_{col} ON audit_rows({col})")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_mode_win ON audit_rows(mode, window_sec)")
        _conn.commit()
    return _conn


def _coerce(v: Any) -> Any:
    if isinstance(v, bool):
        return 1 if v else 0
    return v


def open_row(session_id: str, snapshot: dict[str, Any]) -> bool:
    """Write the immutable decision-time row. Idempotent per session_id. Never raises."""
    try:
        s = snapshot or {}
        execu = s.get("execution") or {}
        regime = s.get("regime") or {}
        pol = s.get("policy") or {}
        sig = s.get("signal") or {}
        vals = {
            "session_id": str(session_id),
            "schema_version": int(s["schema_version"]) if s.get("schema_version") is not None else 1,
            "code_version": s.get("code_version"),
            "mode": s.get("mode"), "slug": s.get("slug"), "epoch": s.get("epoch"),
            "window_sec": s.get("window_sec"), "side": s.get("side"),
            "decision_ts": s.get("decision_ts"),
            "seconds_remaining_at_entry": regime.get("seconds_remaining_at_entry"),
            "entry_minute_in_window": regime.get("entry_minute_in_window"),
            "recommendation": sig.get("recommendation"),
            "weighted_score": sig.get("weighted_score"),
            "confidence_pct": sig.get("confidence_pct"),
            "vol_bucket": regime.get("vol_bucket"),
            "btc_spot_at_entry": execu.get("btc_spot_at_entry"),
            "avg_fill_price": execu.get("avg_fill_price"),
            "contracts": execu.get("contracts"),
            "investment_usd_effective": execu.get("investment_usd_effective") or pol.get("investment_usd_effective"),
            "loss_recovery_multiplier": pol.get("loss_recovery_multiplier"),
            "action_propensity": s.get("action_propensity", 1.0),
            "exploration_flag": _coerce(s.get("exploration_flag", False)),
            "context_json": json.dumps(s, ensure_ascii=False, default=str),
        }
        cols = ",".join(vals.keys())
        ph = ",".join("?" for _ in vals)
        with _LOCK:
            conn = _get_conn()
            conn.execute(
                f"INSERT INTO audit_rows ({cols}) VALUES ({ph}) "
                f"ON CONFLICT(session_id) DO NOTHING",
                [_coerce(v) for v in vals.values()],
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[audit_tracker] open_row failed: {e!r}", flush=True)
        return False


def finalize_row(session_id: str, outcome: dict[str, Any]) -> bool:
    """Append settlement-time + derived columns. Never overwrites decision columns. Never raises."""
    try:
        with _LOCK:
            conn = _get_conn()
            row = conn.execute("SELECT context_json, decision_ts FROM audit_rows WHERE session_id=?",
                               (str(session_id),)).fetchone()
        snapshot = {}
        if row and row["context_json"]:
            try:
                snapshot = json.loads(row["context_json"])
            except Exception:
                snapshot = {}
        derived = audit_derive.derive_learning_fields(snapshot, outcome or {})
        settled_ts = outcome.get("settled_ts") or int(time.time() * 1000)
        update = {
            "settled_ts": int(settled_ts), "exit_type": outcome.get("exit_type"),
            "settlement_status": derived["settlement_status"],
            "realized_pnl": outcome.get("realized_pnl"), "realized_pct": outcome.get("realized_pct"),
            "peak_unrealized_pct": outcome.get("peak_unrealized_pct"),
            "trough_unrealized_pct": outcome.get("trough_unrealized_pct"),
            "hold_duration_sec": outcome.get("hold_duration_sec"), "fees": outcome.get("fees"),
            "settlement_btc_start": outcome.get("settlement_btc_start"),
            "settlement_btc_end": outcome.get("settlement_btc_end"),
            "resolved_outcome": outcome.get("resolved_outcome"),
            "exit_efficiency": derived["exit_efficiency"], "missed_profit_pct": derived["missed_profit_pct"],
            "signal_was_correct": _coerce(derived["signal_was_correct"]),
            "signals_agreement": derived["signals_agreement"],
            "signal_conflict": _coerce(derived["signal_conflict"]),
            "cf_other_side_pnl": derived["cf_other_side_pnl"],
            "dipped_then_won": _coerce(derived["dipped_then_won"]),
            "lesson_tag": derived["lesson_tag"],
            "rule_flags_json": json.dumps(derived["rule_flags"], ensure_ascii=False, default=str),
            "cf_exit_variants_json": json.dumps(derived["cf_exit_variants"], ensure_ascii=False, default=str),
            "pnl_path_json": json.dumps(outcome.get("pnl_path") or [], ensure_ascii=False, default=str),
        }
        sets = ",".join(f"{k}=?" for k in update)
        with _LOCK:
            conn = _get_conn()
            conn.execute(f"UPDATE audit_rows SET {sets} WHERE session_id=?",
                         [*[_coerce(v) for v in update.values()], str(session_id)])
            conn.commit()
        return True
    except Exception as e:
        print(f"[audit_tracker] finalize_row failed: {e!r}", flush=True)
        return False


def _row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for js, key in (("context_json", "context"), ("rule_flags_json", "rule_flags"),
                    ("cf_exit_variants_json", "cf_exit_variants"), ("pnl_path_json", "pnl_path")):
        raw = d.pop(js, None)
        try:
            d[key] = json.loads(raw) if raw else ({} if key != "pnl_path" else [])
        except Exception:
            d[key] = {} if key != "pnl_path" else []
    for b in ("signal_was_correct", "signal_conflict", "dipped_then_won", "exploration_flag"):
        if d.get(b) is not None:
            d[b] = bool(d[b])
    # Expose the signal sub-dict from the stored context snapshot for convenience
    ctx = d.get("context") or {}
    if "signal" not in d:
        d["signal"] = ctx.get("signal") or {}
    return d


def list_audits(*, mode: Optional[str] = None, window_sec: Optional[int] = None,
                settlement_status: Optional[str] = None, side: Optional[str] = None,
                lesson_tag: Optional[str] = None, limit: int = 1000) -> list[dict[str, Any]]:
    try:
        where, args = [], []
        for col, val in (("mode", mode), ("window_sec", window_sec),
                         ("settlement_status", settlement_status), ("side", side),
                         ("lesson_tag", lesson_tag)):
            if val is not None and val != "":
                where.append(f"{col} = ?")
                args.append(val)
        sql = "SELECT * FROM audit_rows"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(settled_ts, decision_ts) DESC LIMIT ?"
        args.append(max(1, min(int(limit), 10000)))
        with _LOCK:  # serialize use of the shared connection (writes may run on a worker thread)
            rows = _get_conn().execute(sql, args).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[audit_tracker] list_audits failed: {e!r}", flush=True)
        return []


def get_audit(session_id: str) -> Optional[dict[str, Any]]:
    try:
        with _LOCK:
            r = _get_conn().execute("SELECT * FROM audit_rows WHERE session_id=?", (str(session_id),)).fetchone()
        return _row_to_dict(r) if r else None
    except Exception as e:
        print(f"[audit_tracker] get_audit failed: {e!r}", flush=True)
        return None


def audit_counts() -> dict[str, Any]:
    try:
        with _LOCK:  # serialize use of the shared connection (writes may run on a worker thread)
            conn = _get_conn()
            by_status: dict[str, int] = {}
            for r in conn.execute("SELECT settlement_status s, COUNT(*) c FROM audit_rows GROUP BY settlement_status"):
                by_status[r["s"] or "PENDING"] = r["c"]
            eff = conn.execute(
                "SELECT AVG(exit_efficiency) e FROM audit_rows WHERE exit_efficiency IS NOT NULL").fetchone()
            top = [{"lesson_tag": r["lesson_tag"], "n": r["c"]} for r in conn.execute(
                "SELECT lesson_tag, COUNT(*) c FROM audit_rows WHERE lesson_tag IS NOT NULL "
                "GROUP BY lesson_tag ORDER BY c DESC LIMIT 8")]
            total = conn.execute("SELECT COUNT(*) c FROM audit_rows").fetchone()["c"]
        wins, losses = by_status.get("WIN", 0), by_status.get("LOSS", 0)
        win_rate = round(100.0 * wins / (wins + losses), 2) if (wins + losses) else 0.0
        return {"by_status": by_status, "total": int(total or 0), "win_rate_pct": win_rate,
                "avg_exit_efficiency": (round(eff["e"], 4) if eff and eff["e"] is not None else None),
                "top_lessons": top}
    except Exception as e:
        print(f"[audit_tracker] audit_counts failed: {e!r}", flush=True)
        return {"by_status": {}, "total": 0, "win_rate_pct": 0.0, "avg_exit_efficiency": None, "top_lessons": []}


def export_rows(*, since_ts: Optional[int] = None, schema_version: Optional[int] = None,
                labels_only: bool = False, limit: int = 100000) -> list[dict[str, Any]]:
    """Full-fidelity dump for the future AI. labels_only quarantines non-{WIN,LOSS}."""
    try:
        where, args = [], []
        if since_ts is not None:
            where.append("COALESCE(settled_ts, decision_ts) >= ?"); args.append(int(since_ts))
        if schema_version is not None:
            where.append("schema_version = ?"); args.append(int(schema_version))
        if labels_only:
            where.append("settlement_status IN ('WIN','LOSS')")
        sql = "SELECT * FROM audit_rows"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(settled_ts, decision_ts) ASC LIMIT ?"
        args.append(int(limit))
        with _LOCK:  # serialize use of the shared connection (writes may run on a worker thread)
            rows = _get_conn().execute(sql, args).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[audit_tracker] export_rows failed: {e!r}", flush=True)
        return []


def get_meta(k: str) -> Optional[str]:
    try:
        with _LOCK:
            r = _get_conn().execute("SELECT v FROM audit_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None
    except Exception:
        return None


def set_meta(k: str, v: str) -> None:
    try:
        with _LOCK:
            conn = _get_conn()
            conn.execute("INSERT INTO audit_meta (k,v) VALUES (?,?) "
                         "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            conn.commit()
    except Exception as e:
        print(f"[audit_tracker] set_meta failed: {e!r}", flush=True)


def backfill_from_trades(trades: list[dict[str, Any]]) -> int:
    """Project historical closed sessions into audit rows (schema_version=0, null Layer B).

    Idempotent via the 'backfilled_through_ts' marker. Groups by session_id, takes the
    first BUY as the (signal-less) decision row and the last settlement/TP as the outcome.
    Returns how many NEW rows were written.
    """
    try:
        through = float(get_meta("backfilled_through_ts") or 0.0)
        by_sess: dict[str, dict[str, Any]] = {}
        max_ts = through
        for t in trades:
            sid = t.get("session_id")
            ts = float(t.get("ts") or 0.0)
            if not sid or ts <= through:
                continue
            max_ts = max(max_ts, ts)
            b = by_sess.setdefault(str(sid), {"buy": None, "close": None})
            typ = str(t.get("type") or "")
            if typ == "BUY" and b["buy"] is None:
                b["buy"] = t
            if typ in ("SELL_TP", "SETTLE_WIN", "SETTLE_LOSS", "SETTLE_UNKNOWN"):
                b["close"] = t
        written = 0
        for sid, b in by_sess.items():
            buy = b["buy"]
            if buy is None or get_audit(sid) is not None:
                continue
            snap = {
                "schema_version": 0, "code_version": None,
                "mode": "demo" if buy.get("execution") != "live" else "live",
                "side": buy.get("side"), "slug": buy.get("slug"), "epoch": buy.get("epoch"),
                "window_sec": buy.get("window_sec"), "decision_ts": int(float(buy.get("ts") or 0) * 1000),
                "signal": {}, "ta": {}, "clob": {}, "sentiment": {}, "history": {},
                "regime": {}, "policy": {}, "provenance": {"signals_missing": True},
                "execution": {"avg_fill_price": buy.get("price"), "contracts": buy.get("contracts")},
            }
            open_row(sid, snap)
            close = b["close"]
            if close is not None:
                finalize_row(sid, {
                    "type": close.get("type"), "exit_type": "settle" if str(close.get("type")).startswith("SETTLE") else "TP",
                    "realized_pnl": close.get("realized_pnl"),
                    "peak_unrealized_pct": close.get("peak_unrealized_pct"),
                    "trough_unrealized_pct": close.get("trough_unrealized_pct"),
                    "resolved_outcome": close.get("resolved_outcome"),
                    "settlement_btc_start": close.get("settlement_btc_start"),
                    "settlement_btc_end": close.get("settlement_btc_end"),
                    "voided": close.get("voided"), "settlement_error": close.get("settlement_error"),
                    "settled_ts": int(float(close.get("ts") or 0) * 1000),
                    "pnl_path": close.get("pnl_path") or [],
                })
            written += 1
        set_meta("backfilled_through_ts", str(max_ts))
        return written
    except Exception as e:
        print(f"[audit_tracker] backfill failed: {e!r}", flush=True)
        return 0
