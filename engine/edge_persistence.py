"""Edge-Watcher persistence sidecar (recording-only).

A PRIVATE SQLite database (``edge_state.db`` under ``DATA_ROOT``) that tracks the
forward-time persistence of edge candidates. It is kept in its own file — separate
from ``edge_stats.py`` / ``edge_watcher.py`` — so the pure statistical analysis stays
I/O-free and trivially unit-testable.

LOAD-BEARING INVARIANTS (see docs/superpowers/specs/2026-06-08-edge-watcher-design.md):
  * RECORDING-ONLY: this module imports NOTHING from trading code and NEVER opens
    ``audit.db`` (the trade ledger) — not for read, not for write. Its only persistent
    state is the private ``edge_state.db`` sidecar.
  * FORWARD-TIME PERSISTENCE (spec §3.5, the live-safety mechanism): a candidate's
    confirmation streak only advances after ``CONFIRM_SPACING_TRADES`` NEW settled
    trades have accrued since the last advance. A single scan can reach at most
    ``confirmations == 1`` — it can NEVER reach ``MIN_CONFIRMATIONS`` (3) from one
    lucky pass, so a single 60s scan can never print ``confirmed``. A failed-gate scan
    resets the streak to 0 (the streak must be *consecutive*).
  * NEVER RAISES: every public fn is wrapped try/except and returns a safe default
    (0 / False) on any error — mirrors fault_tracker / trade_coach defensive style.

Tables
------
``hypotheses(scan_ts, m_count)``
    One row per scan: the honest per-scan multiplicity ``m`` that fed BH-FDR. Kept for
    audit/forensics of how many hypotheses were tested over time.

``edge_verdicts(slice_key, first_seen_ts, last_max_decision_ts,
                consecutive_confirmations, last_state)``
    One row per candidate slice. ``last_max_decision_ts`` is the monotonic position
    marker (frozen max ``decision_ts`` / settled-trade count) at the last advance;
    ``consecutive_confirmations`` is the forward-OOS streak.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

# Single source of truth for the spacing/streak thresholds lives in edge_watcher;
# we re-declare them here as module constants so the sidecar has no import dependency
# on the analysis layer (keeps the dependency arrow one-way). Keep these in sync with
# edge_watcher.CONFIRM_SPACING_TRADES / MIN_CONFIRMATIONS.
CONFIRM_SPACING_TRADES = 100  # >= this many new settled trades between confirmations
MIN_CONFIRMATIONS = 3         # streak length required before `confirmed` (informational)

# Private sidecar DB — NEVER audit.db. Same DATA_ROOT convention as audit_tracker /
# fault_tracker so it survives Railway restarts on the /data volume.
_DB_PATH = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))) / "edge_state.db"
_conn: Optional[sqlite3.Connection] = None
_LOCK = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Lazy singleton connection to the private sidecar DB (creates schema on first use).

    Uses ``check_same_thread=False`` because the watcher runs on a ``to_thread`` worker;
    the module-level ``_LOCK`` serialises writes. No WAL (matches fault_tracker — single
    writer, avoids extra files on a network volume).
    """
    global _conn
    if _conn is None:
        # Defensive hard guard: this sidecar must NEVER point at the trade ledger.
        if str(_DB_PATH).endswith("audit.db"):
            raise RuntimeError("edge_persistence refuses to open the trade ledger (audit.db)")
        conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hypotheses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_ts REAL NOT NULL,
                m_count INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS edge_verdicts (
                slice_key TEXT PRIMARY KEY,
                first_seen_ts REAL NOT NULL,
                last_max_decision_ts REAL NOT NULL,
                consecutive_confirmations INTEGER NOT NULL DEFAULT 0,
                last_state TEXT
            )
            """
        )
        conn.commit()
        _conn = conn
    return _conn


def record_scan(m_count) -> bool:
    """Persist one scan's honest multiplicity ``m`` into ``hypotheses``. Never raises."""
    try:
        m = int(m_count)
    except (TypeError, ValueError):
        return False
    try:
        with _LOCK:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO hypotheses (scan_ts, m_count) VALUES (?, ?)",
                (time.time(), m),
            )
            conn.commit()
        return True
    except Exception as e:  # never break the watcher / event loop
        print(f"[edge_persistence] record_scan failed: {e!r}", flush=True)
        return False


def bump_confirmation(slice_key, max_decision_ts) -> int:
    """Advance (or hold) a candidate slice's forward-confirmation streak. Never raises.

    Forward-time persistence (spec §3.5):
      * First sighting of ``slice_key`` -> create the row, streak == 1, freeze the
        position marker ``last_max_decision_ts = max_decision_ts``.
      * On a later passing scan, advance to ``streak + 1`` ONLY IF at least
        ``CONFIRM_SPACING_TRADES`` new settled trades have accrued since the frozen
        marker (i.e. ``max_decision_ts - last_max_decision_ts >= CONFIRM_SPACING_TRADES``);
        on advance, re-freeze the marker. Otherwise HOLD the current streak (and do NOT
        move the marker — spacing is measured from the last *advance*, not the last scan).

    A single scan therefore reaches at most 1 — never ``MIN_CONFIRMATIONS`` — so one lucky
    pass can never reach ``confirmed``.

    Returns the current confirmation count (>= 1 on success), or 0 on malformed input/error.
    """
    if slice_key is None:
        return 0
    try:
        key = str(slice_key)
    except Exception:
        return 0
    try:
        pos = float(max_decision_ts)
    except (TypeError, ValueError):
        return 0
    try:
        now = time.time()
        with _LOCK:
            conn = _get_conn()
            row = conn.execute(
                "SELECT consecutive_confirmations, last_max_decision_ts "
                "FROM edge_verdicts WHERE slice_key = ?",
                (key,),
            ).fetchone()

            if row is None:
                # First sighting -> streak starts at 1, freeze the marker.
                conn.execute(
                    "INSERT INTO edge_verdicts "
                    "(slice_key, first_seen_ts, last_max_decision_ts, "
                    " consecutive_confirmations, last_state) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (key, now, pos, "forming"),
                )
                conn.commit()
                return 1

            streak = int(row[0])
            last_marker = float(row[1])

            if streak <= 0:
                # The streak was reset by a failed-gate scan; this sighting restarts it.
                conn.execute(
                    "UPDATE edge_verdicts SET consecutive_confirmations = 1, "
                    "last_max_decision_ts = ?, last_state = ? WHERE slice_key = ?",
                    (pos, "forming", key),
                )
                conn.commit()
                return 1

            if (pos - last_marker) >= CONFIRM_SPACING_TRADES:
                # Enough NEW settled trades have accrued -> advance and re-freeze.
                new_streak = streak + 1
                conn.execute(
                    "UPDATE edge_verdicts SET consecutive_confirmations = ?, "
                    "last_max_decision_ts = ?, last_state = ? WHERE slice_key = ?",
                    (new_streak, pos, "forming", key),
                )
                conn.commit()
                return new_streak

            # Within spacing -> HOLD the streak; do not move the frozen marker.
            return streak
    except Exception as e:
        print(f"[edge_persistence] bump_confirmation failed: {e!r}", flush=True)
        return 0


def reset_confirmation(slice_key) -> bool:
    """Reset a slice's confirmation streak to 0 (a failed-gate scan breaks the streak).

    Safe no-op if the slice was never seen. Never raises.
    """
    if slice_key is None:
        return False
    try:
        key = str(slice_key)
    except Exception:
        return False
    try:
        with _LOCK:
            conn = _get_conn()
            conn.execute(
                "UPDATE edge_verdicts SET consecutive_confirmations = 0, "
                "last_state = ? WHERE slice_key = ?",
                ("reset", key),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[edge_persistence] reset_confirmation failed: {e!r}", flush=True)
        return False


def confirmations(slice_key) -> int:
    """Return the current forward-confirmation streak for a slice (0 if unknown). Never raises."""
    if slice_key is None:
        return 0
    try:
        key = str(slice_key)
    except Exception:
        return 0
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT consecutive_confirmations FROM edge_verdicts WHERE slice_key = ?",
            (key,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except Exception as e:
        print(f"[edge_persistence] confirmations failed: {e!r}", flush=True)
        return 0
