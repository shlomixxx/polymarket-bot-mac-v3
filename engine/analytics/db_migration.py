"""
Phase 1: DB Migration — העברת טריידים מ-JSON ל-SQLite.
יוצר טבלאות trades, sessions, pnl_snapshots ב-history.db
ומספק migration חד-פעמי + incremental sync.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

FEE_RATE = 0.002

_DB_PATH = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent.parent))) / "history.db"
_STATE_PATH = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent.parent))) / "demo_state.json"

_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def ensure_analytics_tables() -> None:
    """יוצר את הטבלאות אם לא קיימות."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            type TEXT NOT NULL,
            side TEXT,
            contracts REAL,
            price REAL,
            fee_est REAL,
            token_id TEXT,
            session_id TEXT,
            epoch INTEGER,
            slug TEXT,
            window_sec INTEGER DEFAULT 300,
            realized_pnl REAL,
            peak_unrealized_pct REAL,
            peak_ts REAL,
            trough_unrealized_pct REAL,
            trough_ts REAL,
            entry_target_usd REAL,
            limit_price REAL,
            effective_investment_usd REAL,
            loss_recovery_multiplier REAL,
            ask_u REAL, bid_u REAL,
            ask_d REAL, bid_d REAL,
            reason TEXT,
            execution TEXT,
            gate TEXT,
            reconcile_origin INTEGER DEFAULT 0,
            settlement_btc_start REAL,
            settlement_btc_end REAL,
            settlement_won INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id);
        CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
        CREATE INDEX IF NOT EXISTS idx_trades_type ON trades(type);
        CREATE INDEX IF NOT EXISTS idx_trades_epoch ON trades(epoch);

        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            side TEXT,
            entry_ts REAL,
            exit_ts REAL,
            exit_type TEXT,
            total_invested_usd REAL,
            total_contracts REAL,
            avg_entry_price REAL,
            realized_pnl REAL,
            duration_sec REAL,
            num_dca_slices INTEGER,
            entry_spread REAL,
            peak_unrealized_pct REAL,
            trough_unrealized_pct REAL,
            loss_recovery_multiplier REAL,
            epoch INTEGER,
            slug TEXT,
            hour_utc INTEGER,
            weekday INTEGER,
            execution TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_exit_type ON sessions(exit_type);
        CREATE INDEX IF NOT EXISTS idx_sessions_side ON sessions(side);
        CREATE INDEX IF NOT EXISTS idx_sessions_entry_ts ON sessions(entry_ts);
        CREATE INDEX IF NOT EXISTS idx_sessions_hour ON sessions(hour_utc);

        CREATE TABLE IF NOT EXISTS pnl_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ts REAL NOT NULL,
            upnl_pct REAL,
            bid REAL,
            balance REAL,
            equity REAL
        );

        CREATE INDEX IF NOT EXISTS idx_pnl_session ON pnl_snapshots(session_id);
    """)
    conn.commit()


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _insert_trade(conn: sqlite3.Connection, t: dict[str, Any]) -> None:
    """Insert a single trade into the trades table."""
    trade_id = t.get("id")
    if not trade_id:
        return
    conn.execute("""
        INSERT OR IGNORE INTO trades
        (id, ts, type, side, contracts, price, fee_est, token_id, session_id,
         epoch, slug, window_sec, realized_pnl, peak_unrealized_pct, peak_ts,
         trough_unrealized_pct, trough_ts, entry_target_usd, limit_price,
         effective_investment_usd, loss_recovery_multiplier,
         ask_u, bid_u, ask_d, bid_d, reason, execution, gate, reconcile_origin,
         settlement_btc_start, settlement_btc_end, settlement_won)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        trade_id,
        _safe_float(t.get("ts")),
        t.get("type"),
        t.get("side"),
        _safe_float(t.get("contracts")),
        _safe_float(t.get("price")),
        _safe_float(t.get("fee_est")),
        t.get("token_id"),
        t.get("session_id"),
        _safe_int(t.get("epoch")),
        t.get("slug"),
        _safe_int(t.get("window_sec")) or 300,
        _safe_float(t.get("realized_pnl")),
        _safe_float(t.get("peak_unrealized_pct")),
        _safe_float(t.get("peak_ts")),
        _safe_float(t.get("trough_unrealized_pct")),
        _safe_float(t.get("trough_ts")),
        _safe_float(t.get("entry_target_usd")),
        _safe_float(t.get("limit_price")),
        _safe_float(t.get("effective_investment_usd")),
        _safe_float(t.get("loss_recovery_multiplier")),
        _safe_float(t.get("ask_u")),
        _safe_float(t.get("bid_u")),
        _safe_float(t.get("ask_d")),
        _safe_float(t.get("bid_d")),
        t.get("reason"),
        t.get("execution"),
        t.get("gate"),
        1 if t.get("reconcile_origin") else 0,
        _safe_float(t.get("settlement_btc_start")),
        _safe_float(t.get("settlement_btc_end")),
        1 if t.get("settlement_won") else (0 if t.get("settlement_won") is False else None),
    ))


def _build_session(session_id: str, trades: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Build a session record from grouped trades."""
    buys = [t for t in trades if t.get("type") == "BUY"]
    exits = [t for t in trades if t.get("type") in
             ("SELL_TP", "EXPIRE_0", "SETTLE_WIN", "SETTLE_LOSS", "SETTLE_UNKNOWN")]

    if not buys:
        return None

    first_buy = buys[0]
    side = first_buy.get("side")
    entry_ts = _safe_float(first_buy.get("ts"))
    epoch = _safe_int(first_buy.get("epoch"))

    # Total invested
    total_invested = sum(_safe_float(b.get("effective_investment_usd")) or
                         (_safe_float(b.get("contracts")) or 0) * (_safe_float(b.get("price")) or 0)
                         for b in buys)
    total_contracts = sum(_safe_float(b.get("contracts")) or 0 for b in buys)
    total_cost = sum((_safe_float(b.get("contracts")) or 0) * (_safe_float(b.get("price")) or 0)
                     for b in buys)
    avg_entry = total_cost / total_contracts if total_contracts > 0 else None

    # Exit info
    exit_type = None
    exit_ts = None
    realized_pnl = None
    peak_pct = None
    trough_pct = None

    if exits:
        last_exit = sorted(exits, key=lambda x: _safe_float(x.get("ts")) or 0)[-1]
        exit_ts = _safe_float(last_exit.get("ts"))
        exit_type_raw = last_exit.get("type", "")
        if exit_type_raw == "SELL_TP":
            exit_type = "TP"
        elif exit_type_raw == "EXPIRE_0":
            exit_type = "EXPIRE"
        elif exit_type_raw == "SETTLE_WIN":
            exit_type = "SETTLE_WIN"
        elif exit_type_raw == "SETTLE_LOSS":
            exit_type = "SETTLE_LOSS"
        else:
            exit_type = exit_type_raw

        realized_pnl = _safe_float(last_exit.get("realized_pnl"))
        peak_pct = _safe_float(last_exit.get("peak_unrealized_pct"))
        trough_pct = _safe_float(last_exit.get("trough_unrealized_pct"))

    duration = (exit_ts - entry_ts) if (exit_ts and entry_ts) else None

    # Entry spread
    entry_spread = None
    if side == "Up":
        a = _safe_float(first_buy.get("ask_u"))
        b = _safe_float(first_buy.get("bid_u"))
        if a is not None and b is not None:
            entry_spread = a - b
    elif side == "Down":
        a = _safe_float(first_buy.get("ask_d"))
        b = _safe_float(first_buy.get("bid_d"))
        if a is not None and b is not None:
            entry_spread = a - b

    # Hour/weekday from epoch
    hour_utc = None
    weekday = None
    if epoch:
        import datetime
        dt = datetime.datetime.utcfromtimestamp(epoch)
        hour_utc = dt.hour
        weekday = dt.weekday()

    return {
        "session_id": session_id,
        "side": side,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "exit_type": exit_type,
        "total_invested_usd": total_invested,
        "total_contracts": total_contracts,
        "avg_entry_price": avg_entry,
        "realized_pnl": realized_pnl,
        "duration_sec": duration,
        "num_dca_slices": len(buys),
        "entry_spread": entry_spread,
        "peak_unrealized_pct": peak_pct,
        "trough_unrealized_pct": trough_pct,
        "loss_recovery_multiplier": _safe_float(first_buy.get("loss_recovery_multiplier")),
        "epoch": epoch,
        "slug": first_buy.get("slug"),
        "hour_utc": hour_utc,
        "weekday": weekday,
        "execution": first_buy.get("execution"),
    }


def _insert_session(conn: sqlite3.Connection, s: dict[str, Any]) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO sessions
        (session_id, side, entry_ts, exit_ts, exit_type, total_invested_usd,
         total_contracts, avg_entry_price, realized_pnl, duration_sec,
         num_dca_slices, entry_spread, peak_unrealized_pct, trough_unrealized_pct,
         loss_recovery_multiplier, epoch, slug, hour_utc, weekday, execution)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        s["session_id"], s["side"], s["entry_ts"], s["exit_ts"], s["exit_type"],
        s["total_invested_usd"], s["total_contracts"], s["avg_entry_price"],
        s["realized_pnl"], s["duration_sec"], s["num_dca_slices"],
        s["entry_spread"], s["peak_unrealized_pct"], s["trough_unrealized_pct"],
        s["loss_recovery_multiplier"], s["epoch"], s["slug"],
        s["hour_utc"], s["weekday"], s["execution"],
    ))


def _insert_pnl_snapshots(conn: sqlite3.Connection, session_id: str, pnl_path: list[dict]) -> None:
    """Insert pnl_path snapshots for a session."""
    if not pnl_path:
        return
    rows = [
        (session_id, _safe_float(p.get("ts")), _safe_float(p.get("upnl_pct")),
         _safe_float(p.get("bid")), _safe_float(p.get("balance")), _safe_float(p.get("equity")))
        for p in pnl_path
    ]
    conn.executemany("""
        INSERT INTO pnl_snapshots (session_id, ts, upnl_pct, bid, balance, equity)
        VALUES (?,?,?,?,?,?)
    """, rows)


def migrate_json_to_sqlite(state_path: Optional[str] = None) -> dict[str, int]:
    """One-time migration: reads demo_state.json, populates trades + sessions + pnl_snapshots."""
    path = Path(state_path) if state_path else _STATE_PATH
    if not path.exists():
        return {"trades": 0, "sessions": 0, "pnl_snapshots": 0}

    data = json.loads(path.read_text(encoding="utf-8"))
    all_trades = data.get("trades", [])

    ensure_analytics_tables()
    conn = _get_conn()

    # Insert all trades
    trade_count = 0
    for t in all_trades:
        _insert_trade(conn, t)
        trade_count += 1

    # Group by session
    from collections import defaultdict
    by_session: dict[str, list[dict]] = defaultdict(list)
    for t in all_trades:
        if t.get("reconcile_origin"):
            continue
        sid = t.get("session_id")
        if not sid:
            if t.get("type") == "BUY" and t.get("id"):
                sid = str(t["id"])
            else:
                continue
        by_session[sid].append(t)

    session_count = 0
    pnl_count = 0
    for sid, trades in by_session.items():
        trades.sort(key=lambda x: _safe_float(x.get("ts")) or 0)
        session = _build_session(sid, trades)
        if session:
            _insert_session(conn, session)
            session_count += 1

        # pnl_path from exit trades
        for t in trades:
            pp = t.get("pnl_path")
            if pp:
                _insert_pnl_snapshots(conn, sid, pp)
                pnl_count += len(pp)

    conn.commit()
    return {"trades": trade_count, "sessions": session_count, "pnl_snapshots": pnl_count}


def sync_new_trade(trade: dict[str, Any], all_session_trades: Optional[list[dict]] = None) -> None:
    """Incremental: insert a single new trade and update its session."""
    ensure_analytics_tables()
    conn = _get_conn()
    _insert_trade(conn, trade)

    # Rebuild session if we have the full session trades
    if all_session_trades:
        sid = trade.get("session_id")
        if sid:
            session = _build_session(sid, all_session_trades)
            if session:
                _insert_session(conn, session)
            pp = trade.get("pnl_path")
            if pp:
                # Clear old snapshots for this session and re-insert
                conn.execute("DELETE FROM pnl_snapshots WHERE session_id=?", (sid,))
                for t in all_session_trades:
                    if t.get("pnl_path"):
                        _insert_pnl_snapshots(conn, sid, t["pnl_path"])

    conn.commit()


def get_analytics_db_stats() -> dict[str, Any]:
    """Returns counts for all analytics tables."""
    ensure_analytics_tables()
    conn = _get_conn()
    trades = conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()["c"]
    sessions = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
    closed = conn.execute("SELECT COUNT(*) as c FROM sessions WHERE exit_type IS NOT NULL").fetchone()["c"]
    snaps = conn.execute("SELECT COUNT(*) as c FROM pnl_snapshots").fetchone()["c"]
    return {
        "trades": trades,
        "sessions": sessions,
        "closed_sessions": closed,
        "pnl_snapshots": snaps,
    }
