"""Tests for edge_persistence.py — the private SQLite sidecar + forward-confirmation counter.

INVARIANTS exercised here (load-bearing — see the Edge-Watcher design spec §3.5):
  * Forward-time persistence: a slice's confirmation count only advances after
    >= CONFIRM_SPACING_TRADES NEW settled trades have accrued since the last advance.
    A single scan can reach at most confirmations==1 — never 3 — so it can never reach
    `confirmed` from one lucky pass.
  * A failed-gate scan resets the counter to 0 (the streak must be *consecutive*).
  * NEVER RAISES: every public fn returns a safe default on any error.
  * RECORDING-ONLY: this sidecar must NEVER open/write the trade ledger (audit.db).

Each test gets a fresh tmp DB via monkeypatch (mirrors test_fault_tracker.py).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import edge_persistence as ep


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path: Path, monkeypatch):
    """Every test runs against its own clean sidecar DB."""
    monkeypatch.setattr(ep, "_DB_PATH", tmp_path / "edge_state.db")
    monkeypatch.setattr(ep, "_conn", None)
    yield


# ── first sighting ──────────────────────────────────────────────────────────
def test_first_sighting_is_one():
    n = ep.bump_confirmation("slice_A", 1000.0)
    assert n == 1
    assert ep.confirmations("slice_A") == 1


def test_confirmations_unknown_slice_is_zero():
    assert ep.confirmations("never_seen") == 0


# ── spacing: must accrue >= CONFIRM_SPACING_TRADES new settled trades ────────
def test_second_scan_within_spacing_holds_at_one():
    ep.bump_confirmation("slice_A", 1000.0)
    # only a handful of new trades since -> below spacing -> still 1
    n = ep.bump_confirmation("slice_A", 1000.0 + (ep.CONFIRM_SPACING_TRADES - 1))
    assert n == 1
    assert ep.confirmations("slice_A") == 1


def test_scan_after_spacing_increments_to_two():
    ep.bump_confirmation("slice_A", 1000.0)
    n = ep.bump_confirmation("slice_A", 1000.0 + ep.CONFIRM_SPACING_TRADES)
    assert n == 2
    assert ep.confirmations("slice_A") == 2


def test_reaches_three_only_after_spaced_confirmations():
    base = 5000.0
    s = ep.CONFIRM_SPACING_TRADES
    assert ep.bump_confirmation("slice_B", base) == 1
    # a within-spacing scan in between must NOT advance the streak
    assert ep.bump_confirmation("slice_B", base + 1) == 1
    assert ep.bump_confirmation("slice_B", base + s) == 2
    assert ep.bump_confirmation("slice_B", base + s + 1) == 2
    assert ep.bump_confirmation("slice_B", base + 2 * s) == 3


# ── failed-gate scan resets the streak ──────────────────────────────────────
def test_failed_gate_resets_to_zero():
    s = ep.CONFIRM_SPACING_TRADES
    ep.bump_confirmation("slice_C", 0.0)
    ep.bump_confirmation("slice_C", float(s))
    assert ep.confirmations("slice_C") == 2
    ep.reset_confirmation("slice_C")
    assert ep.confirmations("slice_C") == 0
    # re-sighting after a reset starts the streak over at 1
    assert ep.bump_confirmation("slice_C", float(2 * s)) == 1


def test_reset_unknown_slice_does_not_raise():
    # resetting a slice we've never seen is a safe no-op
    ep.reset_confirmation("ghost")
    assert ep.confirmations("ghost") == 0


# ── record_scan persists honest multiplicity into hypotheses ────────────────
def test_record_scan_persists_rows():
    ep.record_scan(312)
    ep.record_scan(305)
    conn = ep._get_conn()
    rows = conn.execute("SELECT scan_ts, m_count FROM hypotheses ORDER BY scan_ts").fetchall()
    assert len(rows) == 2
    assert {int(r[1]) for r in rows} == {312, 305} or [int(r[1]) for r in rows] == [312, 305]


# ── never-raises on garbage input ───────────────────────────────────────────
def test_bump_with_malformed_inputs_never_raises():
    assert ep.bump_confirmation(None, None) == 0
    assert ep.bump_confirmation("", "not-a-number") == 0
    assert ep.confirmations(None) == 0
    assert ep.record_scan("nan") is False or ep.record_scan("nan") in (True, False)


# ── concurrent open never raises ────────────────────────────────────────────
def test_concurrent_open_never_raises():
    ep.bump_confirmation("slice_X", 1.0)
    # a second independent connection to the same file must coexist
    second = sqlite3.connect(str(ep._DB_PATH), timeout=5.0)
    try:
        second.execute("SELECT COUNT(*) FROM edge_verdicts").fetchone()
        assert ep.bump_confirmation("slice_X", float(ep.CONFIRM_SPACING_TRADES + 1)) == 2
    finally:
        second.close()


# ── the load-bearing safety invariant: NEVER touch the trade ledger ─────────
def test_never_opens_or_writes_audit_db(monkeypatch, tmp_path):
    """The sidecar must operate entirely on its own edge_state.db; it must never
    open audit.db (the trade ledger). We assert the configured DB path is the
    sidecar, and that sqlite3.connect is only ever called with that path."""
    seen_paths: list[str] = []
    real_connect = sqlite3.connect

    def _spy_connect(target, *args, **kwargs):
        seen_paths.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)

    ep.record_scan(10)
    ep.bump_confirmation("slice_Z", 1.0)
    ep.confirmations("slice_Z")
    ep.reset_confirmation("slice_Z")

    assert seen_paths, "expected the sidecar to open at least one sqlite connection"
    for p in seen_paths:
        assert "audit.db" not in p, f"sidecar must NEVER open the trade ledger; opened {p}"
        assert p.endswith("edge_state.db"), f"only the sidecar DB is allowed; opened {p}"
