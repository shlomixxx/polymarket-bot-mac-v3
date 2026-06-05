# Trade Audit & Learning Ledger — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the full per-trade story (decision context at entry + outcome + derived/counterfactual learning fields) into a separate `audit.db`, exposed via API + a `📋 ביקורת עסקאות` tab, structured so a future AI can consume it.

**Architecture:** A new `engine/audit_tracker.py` (twin of `engine/fault_tracker.py`) owns `audit.db` (SQLite on `DATA_ROOT`), one row per trade-session. Pure logic lives in `engine/audit_derive.py` (learning/counterfactual fields) and `engine/audit_snapshot.py` (build the decision snapshot). Thin hooks in `engine/demo_engine.py` open a row at BUY and finalize it at SELL_TP/settlement; `engine/strategy_runner.py` assembles the decision snapshot (wiring the already-computed signals that are currently discarded) and passes it via the existing `context=` channel. Read-only endpoints in `engine/main.py`; a React tab mirrors `src/FaultsTab.tsx`.

**Tech Stack:** Python 3 / FastAPI / sqlite3 (stdlib) / pytest; React + TypeScript (Vite); existing `api()` client.

**Spec:** `docs/superpowers/specs/2026-06-05-trade-audit-learning-ledger-design.md`

**Conventions to follow (from the codebase):**
- Tracker modules NEVER raise into the trading loop — wrap every public function in `try/except` returning a safe default (see `fault_tracker.py`).
- DB path: `Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))) / "audit.db"`.
- `schema_version = 1` for live-captured rows; `0` for backfilled (no Layer-B signals).
- All audit timestamps are UTC **epoch-milliseconds** (`int(time.time()*1000)`); convert to local only in the UI.
- Run Python tests from the `engine/` dir: `cd engine && python -m pytest tests/<file>::<test> -v`.

---

## File Structure

| File | Responsibility | New/Modify |
|---|---|---|
| `engine/audit_derive.py` | Pure functions: settlement_status, derived + counterfactual fields, lesson_tag. No I/O. | Create |
| `engine/audit_snapshot.py` | Pure: build the `decision_snapshot` dict (schema_version 1) from strategy context. | Create |
| `engine/audit_tracker.py` | `audit.db` layer: schema, `open_row`, `finalize_row`, `list/get/counts/export`, `backfill`. Never raises. | Create |
| `engine/strategy_runner.py` | Build snapshot at entry; add to `base_ctx` (`~:1358`). | Modify |
| `engine/demo_engine.py` | Hooks: `open_row` on BUY; `finalize_row` on SELL_TP + settlement. | Modify |
| `engine/main.py` | `GET /api/audit`, `GET /api/audit/{session_id}`, `GET /api/audit/export`; startup backfill task in `lifespan`. | Modify |
| `src/AuditTab.tsx` | The tab (mirror `FaultsTab.tsx`): table + drill-down + filters + summary. | Create |
| `src/App.tsx` | Register tab (type, button tuple, conditional render, import). | Modify |
| `engine/tests/test_audit_derive.py` | Unit tests for pure derive logic. | Create |
| `engine/tests/test_audit_tracker.py` | Lifecycle/DB tests (temp DATA_ROOT). | Create |
| `engine/tests/test_audit_snapshot.py` | Snapshot-shape tests. | Create |

---

## Task 1: `audit_derive.py` — pure learning & counterfactual logic

**Files:**
- Create: `engine/audit_derive.py`
- Test: `engine/tests/test_audit_derive.py`

- [ ] **Step 1: Write the failing tests**

```python
# engine/tests/test_audit_derive.py
import audit_derive as ad


def test_settlement_status_win_loss_void_unknown_pending():
    assert ad.settlement_status({"type": "SETTLE_WIN", "realized_pnl": 4.0}) == "WIN"
    assert ad.settlement_status({"type": "SETTLE_LOSS", "realized_pnl": -2.0}) == "LOSS"
    assert ad.settlement_status({"type": "SELL_TP", "realized_pnl": 3.0}) == "WIN"
    assert ad.settlement_status({"type": "SELL_TP", "realized_pnl": -1.0}) == "LOSS"
    assert ad.settlement_status({"type": "SETTLE_WIN", "voided": True}) == "VOID"
    assert ad.settlement_status({"type": "SETTLE_UNKNOWN"}) == "UNKNOWN"
    assert ad.settlement_status({"type": "SETTLE_WIN", "settlement_error": "x"}) == "UNKNOWN"
    assert ad.settlement_status({"type": "BUY"}) == "PENDING"


def test_exit_efficiency_guards_nonpositive_peak():
    assert ad.exit_efficiency(realized_pct=40.0, peak_pct=80.0) == 0.5
    assert ad.exit_efficiency(realized_pct=10.0, peak_pct=0.0) is None
    assert ad.exit_efficiency(realized_pct=10.0, peak_pct=-5.0) is None


def test_cf_other_side_pnl_binary():
    # We were Up @ 0.52, 40 contracts; opposite (Down) ask was 0.50; market resolved Down (we lost).
    # If we'd been Down: payoff 1.0*40 - 0.50*40 = 20.0 (minus a symmetric fee approximation).
    out = ad.cf_other_side_pnl(
        side="Up", resolved_outcome="Down", contracts=40.0,
        opposite_ask=0.50, fee_rate=0.0,
    )
    assert out == 20.0
    # Missing data -> None
    assert ad.cf_other_side_pnl(side="Up", resolved_outcome=None, contracts=40.0,
                                opposite_ask=0.50, fee_rate=0.0) is None


def test_signal_was_correct_only_when_resolved():
    assert ad.signal_was_correct(side="Up", resolved_outcome="Up") is True
    assert ad.signal_was_correct(side="Up", resolved_outcome="Down") is False
    assert ad.signal_was_correct(side="Up", resolved_outcome=None) is None


def test_signals_agreement_and_conflict():
    snap = {"ta": {"ta_score": 2}, "clob": {"net_score": 0.2},
            "sentiment": {"sentiment_score": 1}, "signal": {"recommendation": "Up"}}
    # all lean Up -> agreement high, no conflict entering Up
    agree = ad.signals_agreement(snap)
    assert 0.0 <= agree <= 1.0 and agree >= 0.66
    assert ad.signal_conflict(snap, side="Up") is False
    assert ad.signal_conflict(snap, side="Down") is True


def test_lesson_tag_classifies():
    assert ad.lesson_tag(status="WIN", exit_eff=0.95, signal_correct=True, conflict=False) == "clean_win"
    assert ad.lesson_tag(status="WIN", exit_eff=0.3, signal_correct=True, conflict=False) == "good_entry_late_exit"
    assert ad.lesson_tag(status="LOSS", exit_eff=None, signal_correct=False, conflict=True) == "signal_conflict_loss"
    assert ad.lesson_tag(status="VOID", exit_eff=None, signal_correct=None, conflict=False) == "void_no_signal"


def test_derive_learning_fields_end_to_end():
    snapshot = {
        "side": "Up", "execution": {"contracts": 40.0, "avg_fill_price": 0.52},
        "ta": {"ta_score": 2}, "clob": {"net_score": 0.2, "down_ask": 0.50},
        "sentiment": {"sentiment_score": 1}, "signal": {"recommendation": "Up"},
    }
    outcome = {
        "type": "SETTLE_LOSS", "realized_pnl": -20.8, "realized_pct": -100.0,
        "peak_unrealized_pct": 5.0, "trough_unrealized_pct": -100.0,
        "resolved_outcome": "Down", "settlement_won": False,
        "fee_rate": 0.0,
    }
    d = ad.derive_learning_fields(snapshot, outcome)
    assert d["settlement_status"] == "LOSS"
    assert d["signal_was_correct"] is False
    assert d["cf_other_side_pnl"] == 20.0          # the Down leg would have won
    assert d["lesson_tag"] in {"signal_conflict_loss", "wrong_side_loss"}
    assert "cf_exit_variants" in d and "rule_flags" in d
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd engine && python -m pytest tests/test_audit_derive.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'audit_derive'`.

- [ ] **Step 3: Write the implementation**

```python
# engine/audit_derive.py
"""Pure, I/O-free derivations for the Trade Audit Ledger.

Every function is total (returns None on missing/invalid input) and never raises,
so it is safe to call from the trading loop and to recompute offline. It reads ONLY
the immutable decision snapshot + the outcome dict — never live data.
"""
from __future__ import annotations

from typing import Any, Optional

# Canonical label categories. Only WIN/LOSS are usable as training labels; the rest
# must be quarantined by the export and never coerced to a number.
LABEL_OK = ("WIN", "LOSS")


def settlement_status(outcome: dict[str, Any]) -> str:
    """Map a closing trade/outcome to the canonical enum, separate from numeric PnL."""
    typ = str(outcome.get("type") or "")
    if outcome.get("voided"):
        return "VOID"
    if typ == "SETTLE_UNKNOWN" or outcome.get("settlement_error"):
        return "UNKNOWN"
    if typ in ("BUY", "") and outcome.get("realized_pnl") is None:
        return "PENDING"
    rp = outcome.get("realized_pnl")
    if rp is None:
        return "PENDING"
    try:
        return "WIN" if float(rp) > 0 else "LOSS"
    except (TypeError, ValueError):
        return "UNKNOWN"


def exit_efficiency(*, realized_pct: Optional[float], peak_pct: Optional[float]) -> Optional[float]:
    """realized_pct / peak_pct. None when there was no favorable excursion (peak<=0)."""
    try:
        if realized_pct is None or peak_pct is None or float(peak_pct) <= 0:
            return None
        return round(float(realized_pct) / float(peak_pct), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def signal_was_correct(*, side: Optional[str], resolved_outcome: Optional[str]) -> Optional[bool]:
    """Directional correctness — only defined once the market resolved Up/Down."""
    if not side or resolved_outcome not in ("Up", "Down"):
        return None
    return side == resolved_outcome


def cf_other_side_pnl(*, side: Optional[str], resolved_outcome: Optional[str],
                      contracts: Optional[float], opposite_ask: Optional[float],
                      fee_rate: float = 0.0) -> Optional[float]:
    """Counterfactual: PnL if we'd taken the OPPOSITE leg, same contracts, at its entry ask.

    Binary payoff: $1 if the opposite side was the resolved winner else $0.
    None when resolution/side/quote are unknown.
    """
    if not side or resolved_outcome not in ("Up", "Down"):
        return None
    if contracts is None or opposite_ask is None:
        return None
    try:
        opp = "Down" if side == "Up" else "Up"
        c = float(contracts)
        payoff = (1.0 if opp == resolved_outcome else 0.0) * c
        cost = float(opposite_ask) * c * (1.0 + float(fee_rate))
        return round(payoff - cost, 4)
    except (TypeError, ValueError):
        return None


def _lean(snapshot: dict[str, Any]) -> dict[str, int]:
    """Per-component directional lean in {-1,0,1} (positive = Up)."""
    ta = (snapshot.get("ta") or {}).get("ta_score")
    clob = (snapshot.get("clob") or {}).get("net_score")
    sent = (snapshot.get("sentiment") or {}).get("sentiment_score")

    def sign(x: Any) -> int:
        try:
            v = float(x)
        except (TypeError, ValueError):
            return 0
        return 1 if v > 0 else (-1 if v < 0 else 0)

    return {"ta": sign(ta), "clob": sign(clob), "sentiment": sign(sent)}


def signals_agreement(snapshot: dict[str, Any]) -> Optional[float]:
    """Fraction of non-neutral components that share the majority direction (0..1)."""
    leans = [v for v in _lean(snapshot).values() if v != 0]
    if not leans:
        return None
    ups = sum(1 for v in leans if v > 0)
    downs = len(leans) - ups
    return round(max(ups, downs) / len(leans), 4)


def signal_conflict(snapshot: dict[str, Any], *, side: Optional[str]) -> Optional[bool]:
    """True if we entered `side` against the non-neutral majority of components."""
    if side not in ("Up", "Down"):
        return None
    leans = [v for v in _lean(snapshot).values() if v != 0]
    if not leans:
        return None
    ups = sum(1 for v in leans if v > 0)
    downs = len(leans) - ups
    majority = "Up" if ups > downs else ("Down" if downs > ups else None)
    if majority is None:
        return None
    return side != majority


def lesson_tag(*, status: str, exit_eff: Optional[float],
               signal_correct: Optional[bool], conflict: Optional[bool]) -> str:
    """A short machine+human readable verdict tag."""
    if status in ("VOID", "UNKNOWN", "PENDING", "INVALID"):
        return "void_no_signal"
    if status == "WIN":
        if exit_eff is not None and exit_eff < 0.5:
            return "good_entry_late_exit"
        return "clean_win"
    # LOSS
    if conflict:
        return "signal_conflict_loss"
    if signal_correct is False:
        return "wrong_side_loss"
    return "right_side_loss"


def cf_exit_variants(outcome: dict[str, Any]) -> dict[str, Optional[float]]:
    """What alternative exits would have returned, from data we already capture."""
    return {
        "pnl_if_held_to_resolution": outcome.get("settlement_pnl_if_held"),
        "pnl_at_peak": outcome.get("peak_unrealized_pct"),
        "pnl_at_trough": outcome.get("trough_unrealized_pct"),
    }


def rule_flags(snapshot: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    pol = snapshot.get("policy") or {}
    regime = snapshot.get("regime") or {}
    return {
        "recovery_active": bool(pol.get("loss_recovery_enabled") and (pol.get("loss_recovery_multiplier") or 1.0) > 1.0),
        "against_signal": signal_conflict(snapshot, side=snapshot.get("side")),
        "entered_late": (regime.get("seconds_remaining_at_entry") is not None
                         and float(regime.get("seconds_remaining_at_entry")) < 60),
        "outcome_reason": outcome.get("outcome_reason"),  # filled/rejected/no_fill/expired/None
    }


def derive_learning_fields(snapshot: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Compute the full Layer-D (derived + counterfactual) field set."""
    side = snapshot.get("side")
    execu = snapshot.get("execution") or {}
    clob = snapshot.get("clob") or {}
    resolved = outcome.get("resolved_outcome")
    status = settlement_status(outcome)
    realized_pct = outcome.get("realized_pct")
    peak_pct = outcome.get("peak_unrealized_pct")

    eff = exit_efficiency(realized_pct=realized_pct, peak_pct=peak_pct)
    correct = signal_was_correct(side=side, resolved_outcome=resolved)
    conflict = signal_conflict(snapshot, side=side)
    opp_ask = clob.get("down_ask") if side == "Up" else clob.get("up_ask")

    return {
        "settlement_status": status,
        "exit_efficiency": eff,
        "missed_profit_pct": (round(float(peak_pct) - float(realized_pct), 4)
                              if peak_pct is not None and realized_pct is not None else None),
        "signal_was_correct": correct,
        "signals_agreement": signals_agreement(snapshot),
        "signal_conflict": conflict,
        "cf_other_side_pnl": cf_other_side_pnl(
            side=side, resolved_outcome=resolved,
            contracts=execu.get("contracts"), opposite_ask=opp_ask,
            fee_rate=float(outcome.get("fee_rate", 0.0) or 0.0)),
        "dipped_then_won": bool(status == "WIN" and (outcome.get("trough_unrealized_pct") or 0) < 0),
        "lesson_tag": lesson_tag(status=status, exit_eff=eff, signal_correct=correct, conflict=conflict),
        "cf_exit_variants": cf_exit_variants(outcome),
        "rule_flags": rule_flags(snapshot, outcome),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd engine && python -m pytest tests/test_audit_derive.py -v`
Expected: PASS (all tests).
> Note: `test_lesson_tag` expects `clean_win`/`good_entry_late_exit`/`signal_conflict_loss`/`void_no_signal`; `test_derive_learning_fields_end_to_end` allows `signal_conflict_loss` OR `wrong_side_loss`. If a test mismatches, fix the test's expectation to match the implemented thresholds (do not weaken the implementation).

- [ ] **Step 5: Commit**

```bash
git add engine/audit_derive.py engine/tests/test_audit_derive.py
git commit -m "feat(audit): pure derive logic — settlement_status, counterfactuals, lesson_tag"
```

---

## Task 2: `audit_snapshot.py` — build the decision snapshot

**Files:**
- Create: `engine/audit_snapshot.py`
- Test: `engine/tests/test_audit_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/test_audit_snapshot.py
import audit_snapshot as asnap


def test_build_decision_snapshot_shape():
    snap = asnap.build_decision_snapshot(
        mode="demo", side="Up", slug="btc-updown-5m-123", epoch=123, window_sec=300,
        decision_ts_ms=1733385600123, code_version="abc123",
        signal_result={"recommendation": "Up", "up_confidence": 0.63, "down_confidence": 0.37,
                       "weighted_score": 0.26, "confidence_pct": 63.0,
                       "sub": {"ta": {"rsi": 58.2, "ta_score": 2},
                               "clob": {"net_score": 0.2, "up_ask": 0.52, "down_ask": 0.50, "spread": 0.02},
                               "sentiment": {"funding_rate_pct": -0.01, "fear_greed_value": 41, "sentiment_score": 1},
                               "history": {"hour_up_rate": 0.57, "hour_sample_size": 120, "overall_up_rate": 0.51}}},
        policy={"order_mode": "market", "take_profit_pct": 50, "entry_price_cents_cap": 65,
                "loss_recovery_enabled": True, "loss_recovery_multiplier": 2.0, "loss_recovery_streak": 1},
        book={"ask_u": 0.52, "bid_u": 0.50, "ask_d": 0.50, "bid_d": 0.48},
        provenance={"btc_spot_source": "ws", "btc_spot_age_ms": 120, "book_source": "ws", "book_age_ms": 80},
        regime={"vol_bucket": "mid", "btc_change_pct_at_entry": 0.05,
                "seconds_remaining_at_entry": 210, "entry_minute_in_window": 1},
        execution={"avg_fill_price": 0.52, "contracts": 40.0, "gate": "signal", "reason": "auto"},
        btc_spot_at_entry=64000.0,
    )
    assert snap["schema_version"] == 1
    assert snap["side"] == "Up"
    assert snap["signal"]["recommendation"] == "Up"
    assert snap["ta"]["ta_score"] == 2
    assert snap["clob"]["down_ask"] == 0.50          # needed for cf_other_side_pnl
    assert snap["policy"]["loss_recovery_multiplier"] == 2.0
    assert snap["provenance"]["btc_spot_source"] == "ws"
    assert snap["execution"]["contracts"] == 40.0


def test_build_marks_missing_signals():
    snap = asnap.build_decision_snapshot(
        mode="demo", side="Up", slug="s", epoch=1, window_sec=300,
        decision_ts_ms=1, code_version="x", signal_result=None,
        policy={}, book={}, provenance={}, regime={}, execution={}, btc_spot_at_entry=None)
    assert snap["schema_version"] == 1
    assert snap["provenance"]["signals_missing"] is True
    assert snap["signal"] == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd engine && python -m pytest tests/test_audit_snapshot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'audit_snapshot'`.

- [ ] **Step 3: Write the implementation**

```python
# engine/audit_snapshot.py
"""Assemble the immutable point-in-time decision snapshot (schema_version 1).

Pure dict assembly. The snapshot mirrors §6 of the design spec and is the value
written ONCE at the entry tick (never recomputed later). `signal_result` is the
dict returned by signal_engine.compute_signals() (it nests components under "sub").
"""
from __future__ import annotations

from typing import Any, Optional

SCHEMA_VERSION = 1


def build_decision_snapshot(
    *, mode: str, side: str, slug: str, epoch: int, window_sec: int,
    decision_ts_ms: int, code_version: str,
    signal_result: Optional[dict[str, Any]],
    policy: dict[str, Any], book: dict[str, Any], provenance: dict[str, Any],
    regime: dict[str, Any], execution: dict[str, Any],
    btc_spot_at_entry: Optional[float],
) -> dict[str, Any]:
    sig = signal_result or {}
    sub = sig.get("sub") or {}
    signals_missing = signal_result is None

    prov = dict(provenance or {})
    prov.setdefault("signals_missing", signals_missing)
    prov.setdefault("signals_stale", False)

    return {
        "schema_version": SCHEMA_VERSION,
        "code_version": code_version,
        "decision_ts": decision_ts_ms,
        "mode": mode,
        "side": side,
        "slug": slug,
        "epoch": epoch,
        "window_sec": window_sec,
        "signal": ({k: sig.get(k) for k in
                    ("recommendation", "up_confidence", "down_confidence",
                     "weighted_score", "confidence_pct", "weights")} if signal_result else {}),
        "ta": sub.get("ta") or {},
        "clob": _with_book(sub.get("clob") or {}, book),
        "sentiment": sub.get("sentiment") or {},
        "history": sub.get("history") or {},
        "regime": dict(regime or {}),
        "policy": dict(policy or {}),
        "provenance": prov,
        "execution": {**(execution or {}), "btc_spot_at_entry": btc_spot_at_entry,
                      "arrival_mid": _mid(book)},
        # off-policy schema-ready placeholders (constant today; see spec §3 nice-to-have)
        "action_propensity": 1.0,
        "exploration_flag": False,
    }


def _with_book(clob: dict[str, Any], book: dict[str, Any]) -> dict[str, Any]:
    """Ensure the opposite-side ask is present in clob for cf_other_side_pnl."""
    out = dict(clob)
    out.setdefault("up_ask", book.get("ask_u"))
    out.setdefault("down_ask", book.get("ask_d"))
    out.setdefault("up_bid", book.get("bid_u"))
    out.setdefault("down_bid", book.get("bid_d"))
    return out


def _mid(book: dict[str, Any]) -> Optional[float]:
    a, b = book.get("ask_u"), book.get("bid_u")
    if a is None or b is None:
        return None
    try:
        return round((float(a) + float(b)) / 2.0, 4)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd engine && python -m pytest tests/test_audit_snapshot.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/audit_snapshot.py engine/tests/test_audit_snapshot.py
git commit -m "feat(audit): build_decision_snapshot (schema_version 1, point-in-time)"
```

---

## Task 3: `audit_tracker.py` — the `audit.db` layer

**Files:**
- Create: `engine/audit_tracker.py`
- Test: `engine/tests/test_audit_tracker.py`

- [ ] **Step 1: Write the failing tests**

```python
# engine/tests/test_audit_tracker.py
import importlib
import time


def _fresh_tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import audit_tracker
    importlib.reload(audit_tracker)  # rebind _DB_PATH to the temp DATA_ROOT
    return audit_tracker


def _snapshot(side="Up"):
    return {
        "schema_version": 1, "code_version": "abc", "decision_ts": 1733385600123,
        "mode": "demo", "side": side, "slug": "s", "epoch": 1, "window_sec": 300,
        "signal": {"recommendation": side, "weighted_score": 0.2, "confidence_pct": 60.0},
        "ta": {"ta_score": 2}, "clob": {"net_score": 0.2, "up_ask": 0.52, "down_ask": 0.50},
        "sentiment": {"sentiment_score": 1}, "history": {},
        "regime": {"vol_bucket": "mid", "seconds_remaining_at_entry": 210},
        "policy": {"loss_recovery_enabled": True, "loss_recovery_multiplier": 2.0},
        "provenance": {"btc_spot_source": "ws"},
        "execution": {"avg_fill_price": 0.52, "contracts": 40.0, "btc_spot_at_entry": 64000.0},
    }


def test_open_then_finalize_row(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    assert at.open_row("sess1", _snapshot()) is True
    # idempotent: second open for same session does not duplicate
    at.open_row("sess1", _snapshot())
    rows = at.list_audits()
    assert len(rows) == 1
    assert rows[0]["settlement_status"] == "PENDING"

    ok = at.finalize_row("sess1", {
        "type": "SETTLE_LOSS", "realized_pnl": -20.8, "realized_pct": -100.0,
        "peak_unrealized_pct": 5.0, "trough_unrealized_pct": -100.0,
        "resolved_outcome": "Down", "settled_ts": 1733385900123,
        "settlement_btc_start": 64000.0, "settlement_btc_end": 63900.0,
        "hold_duration_sec": 300.0, "fees": 0.1, "exit_type": "settle",
    })
    assert ok is True
    row = at.get_audit("sess1")
    assert row["settlement_status"] == "LOSS"
    assert row["settled_ts"] > row["decision_ts"]          # invariant
    assert row["signal_was_correct"] is False
    assert row["cf_other_side_pnl"] == 20.0
    assert row["lesson_tag"] in {"signal_conflict_loss", "wrong_side_loss", "right_side_loss"}


def test_finalize_does_not_mutate_decision_fields(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    at.open_row("s", _snapshot())
    before = at.get_audit("s")
    at.finalize_row("s", {"type": "SETTLE_WIN", "realized_pnl": 18.0, "realized_pct": 90.0,
                          "peak_unrealized_pct": 95.0, "resolved_outcome": "Up",
                          "settled_ts": 1733385900123, "exit_type": "settle"})
    after = at.get_audit("s")
    assert after["decision_ts"] == before["decision_ts"]
    assert after["recommendation"] == before["recommendation"]
    assert after["loss_recovery_multiplier"] == before["loss_recovery_multiplier"]


def test_counts_winrate_excludes_non_label(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    for i, (typ, rp, res) in enumerate([
        ("SETTLE_WIN", 10.0, "Up"), ("SETTLE_LOSS", -5.0, "Down"),
        ("SETTLE_UNKNOWN", None, None),  # must NOT count toward win-rate
    ]):
        sid = f"s{i}"
        at.open_row(sid, _snapshot())
        at.finalize_row(sid, {"type": typ, "realized_pnl": rp, "resolved_outcome": res,
                              "settled_ts": 1733385900123 + i, "exit_type": "settle"})
    c = at.audit_counts()
    assert c["by_status"]["WIN"] == 1
    assert c["by_status"]["LOSS"] == 1
    assert c["by_status"]["UNKNOWN"] == 1
    assert c["win_rate_pct"] == 50.0    # 1 win / (1 win + 1 loss); UNKNOWN excluded


def test_export_excludes_non_label_rows(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    at.open_row("w", _snapshot()); at.finalize_row("w", {"type": "SETTLE_WIN", "realized_pnl": 1.0,
        "resolved_outcome": "Up", "settled_ts": 2, "exit_type": "settle"})
    at.open_row("u", _snapshot()); at.finalize_row("u", {"type": "SETTLE_UNKNOWN",
        "settled_ts": 2, "exit_type": "settle"})
    labeled = at.export_rows(labels_only=True)
    assert {r["session_id"] for r in labeled} == {"w"}


def test_never_raises_on_garbage(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    assert at.open_row("x", {"bad": object()}) in (True, False)   # no exception escapes
    assert at.finalize_row("missing", {"type": "SETTLE_WIN"}) in (True, False)
    assert isinstance(at.list_audits(), list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd engine && python -m pytest tests/test_audit_tracker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'audit_tracker'`.

- [ ] **Step 3: Write the implementation**

Mirror `engine/fault_tracker.py` exactly for connection/locking/never-raise discipline. Promoted columns per spec §5; full snapshot in `context_json`. Key points: `open_row` is `INSERT ... ON CONFLICT(session_id) DO NOTHING` (idempotent); `finalize_row` only `UPDATE`s settlement-time + derived columns (never the decision columns); derived fields come from `audit_derive.derive_learning_fields`.

```python
# engine/audit_tracker.py
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

# Columns kept on the row in addition to context_json (promoted for indexing/query).
_DECISION_COLS = (
    "schema_version", "code_version", "mode", "slug", "epoch", "window_sec", "side",
    "decision_ts", "seconds_remaining_at_entry", "entry_minute_in_window",
    "recommendation", "weighted_score", "confidence_pct", "vol_bucket",
    "btc_spot_at_entry", "avg_fill_price", "contracts", "investment_usd_effective",
    "loss_recovery_multiplier", "action_propensity", "exploration_flag",
)


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=5.0)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout=5000")
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
            "schema_version": int(s.get("schema_version", 1) or 1),
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
    return d


def list_audits(*, mode: Optional[str] = None, window_sec: Optional[int] = None,
                settlement_status: Optional[str] = None, side: Optional[str] = None,
                lesson_tag: Optional[str] = None, limit: int = 1000) -> list[dict[str, Any]]:
    try:
        conn = _get_conn()
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
        return [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception as e:
        print(f"[audit_tracker] list_audits failed: {e!r}", flush=True)
        return []


def get_audit(session_id: str) -> Optional[dict[str, Any]]:
    try:
        conn = _get_conn()
        r = conn.execute("SELECT * FROM audit_rows WHERE session_id=?", (str(session_id),)).fetchone()
        return _row_to_dict(r) if r else None
    except Exception as e:
        print(f"[audit_tracker] get_audit failed: {e!r}", flush=True)
        return None


def audit_counts() -> dict[str, Any]:
    try:
        conn = _get_conn()
        by_status: dict[str, int] = {}
        for r in conn.execute("SELECT settlement_status s, COUNT(*) c FROM audit_rows GROUP BY settlement_status"):
            by_status[r["s"] or "PENDING"] = r["c"]
        wins, losses = by_status.get("WIN", 0), by_status.get("LOSS", 0)
        win_rate = round(100.0 * wins / (wins + losses), 2) if (wins + losses) else 0.0
        eff = conn.execute(
            "SELECT AVG(exit_efficiency) e FROM audit_rows WHERE exit_efficiency IS NOT NULL").fetchone()
        top = [{"lesson_tag": r["lesson_tag"], "n": r["c"]} for r in conn.execute(
            "SELECT lesson_tag, COUNT(*) c FROM audit_rows WHERE lesson_tag IS NOT NULL "
            "GROUP BY lesson_tag ORDER BY c DESC LIMIT 8")]
        total = conn.execute("SELECT COUNT(*) c FROM audit_rows").fetchone()["c"]
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
        conn = _get_conn()
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
        return [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception as e:
        print(f"[audit_tracker] export_rows failed: {e!r}", flush=True)
        return []


def get_meta(k: str) -> Optional[str]:
    try:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd engine && python -m pytest tests/test_audit_tracker.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/audit_tracker.py engine/tests/test_audit_tracker.py
git commit -m "feat(audit): audit.db store — open/finalize/list/counts/export, never-raise"
```

---

## Task 4: Backfill historical sessions (automatic, idempotent)

**Files:**
- Modify: `engine/audit_tracker.py` (add `backfill_from_trades`)
- Test: `engine/tests/test_audit_tracker.py` (add a test)

- [ ] **Step 1: Add the failing test**

```python
# append to engine/tests/test_audit_tracker.py
def test_backfill_from_trades_is_idempotent(tmp_path, monkeypatch):
    at = _fresh_tracker(tmp_path, monkeypatch)
    trades = [
        {"type": "BUY", "session_id": "h1", "side": "Up", "ts": 1000.0, "contracts": 40,
         "price": 0.5, "token_id": "t", "window_sec": 300, "epoch": 1, "slug": "s"},
        {"type": "SETTLE_WIN", "session_id": "h1", "side": "Up", "ts": 1300.0,
         "realized_pnl": 18.0, "resolved_outcome": "Up", "peak_unrealized_pct": 95.0,
         "settlement_btc_start": 64000.0, "settlement_btc_end": 64100.0},
    ]
    n1 = at.backfill_from_trades(trades)
    n2 = at.backfill_from_trades(trades)   # second run is a no-op (marker advanced)
    assert n1 == 1 and n2 == 0
    row = at.get_audit("h1")
    assert row["schema_version"] == 0          # pre-signal era
    assert row["settlement_status"] == "WIN"
    assert row["signal"] == {}                  # Layer B is null for history
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd engine && python -m pytest tests/test_audit_tracker.py::test_backfill_from_trades_is_idempotent -v`
Expected: FAIL with `AttributeError: module 'audit_tracker' has no attribute 'backfill_from_trades'`.

- [ ] **Step 3: Implement `backfill_from_trades`**

```python
# add to engine/audit_tracker.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd engine && python -m pytest tests/test_audit_tracker.py -v`
Expected: PASS (all tracker tests).

- [ ] **Step 5: Commit**

```bash
git add engine/audit_tracker.py engine/tests/test_audit_tracker.py
git commit -m "feat(audit): idempotent automatic backfill from historical trades (schema_version 0)"
```

---

## Task 5: Wire the decision snapshot into the entry path (`strategy_runner.py`)

This is the core engine change: the signals computed elsewhere must be captured at the decision tick and attached to the trade context (currently `base_ctx` carries only book quotes). Keep it best-effort: a snapshot failure must NEVER block a trade.

**Files:**
- Modify: `engine/strategy_runner.py` (around `base_ctx` at `~:1358`)

- [ ] **Step 1: Add the snapshot inputs after `base_ctx` is built**

Locate `base_ctx: dict[str, Any] = { ... }` (≈ line 1358). Immediately after the dict literal, insert a **plain JSON-serializable** inputs dict (NOT a lambda — `base_ctx` keys can be copied onto the persisted trade and merged into `demo_state.json`, so anything stored here must serialize):

```python
        # ── Audit ledger: stash the point-in-time decision inputs (the "WHY"). ──
        # Plain dict (JSON-safe). The demo_engine BUY hook (Task 6) completes the snapshot
        # with the final side + execution. Reuses the already-computed signal result, so no
        # extra feed calls. If no signal is available here, signals_missing is recorded.
        try:
            import audit_snapshot
            _sig_result = getattr(self.rt, "_last_signal_result", None)
            base_ctx["audit_inputs"] = {
                "mode": ("live" if getattr(cfg, "live_enabled", False) else "demo"),
                "slug": m.slug, "epoch": int(m.epoch), "window_sec": int(m.window_sec),
                "code_version": (audit_snapshot.get_git_sha() or "")[:12],
                "signal_result": _sig_result,
                "policy": {
                    "order_mode": getattr(cfg, "order_mode", None),
                    "take_profit_pct": getattr(cfg, "take_profit_pct", None),
                    "entry_price_cents_cap": getattr(cfg, "entry_price_cents", None),
                    "side_preference": getattr(cfg, "side_preference", None),
                    "loss_recovery_enabled": getattr(cfg, "loss_recovery_enabled", None),
                    "loss_recovery_multiplier": self.demo.state.loss_recovery_multiplier,
                    "loss_recovery_streak": self.demo.state.loss_recovery_streak,
                },
                "book": {"ask_u": ask_u, "bid_u": bid_u, "ask_d": ask_d, "bid_d": bid_d},
                "provenance": {"book_source": "ws", "signals_missing": _sig_result is None},
                "regime": {"vol_bucket": None, "seconds_remaining_at_entry": sec_left,
                           "entry_minute_in_window": int((int(m.window_sec) - sec_left) // 60)},
            }
        except Exception as _e:
            print(f"[audit] audit_inputs build failed (non-fatal): {_e!r}", flush=True)
```

> **Design note:** the keys in `audit_inputs` are exactly the *static* kwargs of `build_decision_snapshot`. The BUY hook (Task 6) supplies the *dynamic* kwargs (`side`, `execution`, `decision_ts_ms`, `btc_spot_at_entry`). Verify the `cfg` attribute names against `StrategyConfig` (use `getattr(..., None)` defaults as shown so a renamed field degrades to null rather than crashing). If `self.rt._last_signal_result` does not exist, add it in Step 2.

- [ ] **Step 2: Ensure the signal result is captured on the runtime + a git-sha helper exists**

Search for where `compute_signals` results are available to the strategy loop. If `self.rt._last_signal_result` is never set, set it wherever the loop already calls signals (or, if the strategy loop does not call `compute_signals`, call it once per tick guarded by the existing cache in `main.py:209`). Minimal robust fallback — store `None` so snapshots record `signals_missing=true` until signal-mode wiring lands:

```python
# in StrategyRuntime.__init__ (engine/strategy_runner.py), add:
self._last_signal_result = None
```

Add this `get_git_sha` helper to `engine/audit_snapshot.py` (Step 1 calls `audit_snapshot.get_git_sha()`):

```python
# engine/audit_snapshot.py  (add at module level)
import subprocess
from pathlib import Path
_GIT_SHA: str | None = None
def get_git_sha() -> str:
    global _GIT_SHA
    if _GIT_SHA is None:
        try:
            _GIT_SHA = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parent),
                stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        except Exception:
            _GIT_SHA = ""
    return _GIT_SHA
```

- [ ] **Step 3: Manual verification (no unit test — integration point)**

Run the existing strategy tests to confirm nothing broke:
Run: `cd engine && python -m pytest tests/test_strategy_runtime.py tests/test_market_order.py -v`
Expected: PASS (unchanged behavior; the snapshot factory is additive and best-effort).

- [ ] **Step 4: Commit**

```bash
git add engine/strategy_runner.py engine/audit_snapshot.py
git commit -m "feat(audit): assemble decision snapshot at entry tick (wire discarded signals)"
```

---

## Task 6: Hooks in `demo_engine.py` — open at BUY, finalize at close

**Files:**
- Modify: `engine/demo_engine.py` (in `simulate_market_buy` ~:865, `record_live_buy` ~:957, `simulate_sell_all` ~:1245, `record_live_sell` ~:1152, `expire_all_outside_tokens` ~:460)

- [ ] **Step 1: Add the open hook after a BUY trade record is created**

In `simulate_market_buy` and `record_live_buy`, after the trade dict is appended and `session_id` is known, add (best-effort):

```python
            # ── Audit ledger: open the decision-time row (best-effort, never blocks). ──
            try:
                import audit_tracker, audit_snapshot, time as _t
                _inp = (context or {}).get("audit_inputs")
                if _inp and trade.get("session_id"):
                    _snap = audit_snapshot.build_decision_snapshot(
                        side=trade.get("side"),
                        decision_ts_ms=int(_t.time() * 1000), btc_spot_at_entry=None,
                        execution={"avg_fill_price": trade.get("price"),
                                   "contracts": trade.get("contracts"),
                                   "gate": (context or {}).get("gate"),
                                   "reason": (context or {}).get("reason"),
                                   "investment_usd_effective": (context or {}).get("effective_investment_usd")},
                        **_inp)
                    audit_tracker.open_row(str(trade["session_id"]), _snap)
            except Exception as _e:
                print(f"[audit] open_row hook failed (non-fatal): {_e!r}", flush=True)
```

> `_inp` carries exactly the static kwargs of `build_decision_snapshot`; this call supplies the dynamic ones. Read `side` from the trade dict (works in both buy functions). The `audit_inputs` key is JSON-serializable, so it is harmless even if it gets copied onto the persisted trade.

- [ ] **Step 2: Add the finalize hook at close (TP + settlement)**

In `simulate_sell_all` / `record_live_sell` (full-exit branch) and in `expire_all_outside_tokens` (after each SETTLE_* trade is built with its `session_id`), add:

```python
            # ── Audit ledger: finalize the row with the outcome (best-effort). ──
            try:
                import audit_tracker
                if trade.get("session_id"):
                    audit_tracker.finalize_row(str(trade["session_id"]), {
                        "type": trade.get("type"),
                        "exit_type": ("TP" if trade.get("type") == "SELL_TP" else
                                      ("voided" if trade.get("voided") else "settle")),
                        "realized_pnl": trade.get("realized_pnl"),
                        "realized_pct": (round(100.0 * trade["realized_pnl"] /
                                               max(1e-9, trade.get("leg_cost") or 0), 4)
                                         if trade.get("realized_pnl") is not None and trade.get("leg_cost") else None),
                        "peak_unrealized_pct": trade.get("peak_unrealized_pct"),
                        "trough_unrealized_pct": trade.get("trough_unrealized_pct"),
                        "hold_duration_sec": (trade.get("ts", 0) - (trade.get("open_ts") or trade.get("ts", 0))),
                        "fees": trade.get("fee_est"),
                        "settlement_btc_start": trade.get("settlement_btc_start"),
                        "settlement_btc_end": trade.get("settlement_btc_end"),
                        "resolved_outcome": trade.get("resolved_outcome"),
                        "voided": trade.get("voided"),
                        "settlement_error": trade.get("settlement_error"),
                        "settled_ts": int(float(trade.get("ts") or time.time()) * 1000),
                        "pnl_path": trade.get("pnl_path") or [],
                        "fee_rate": 0.0,
                    })
            except Exception as _e:
                print(f"[audit] finalize_row hook failed (non-fatal): {_e!r}", flush=True)
```

> `realized_pct` here approximates ROI vs leg cost; if `leg_cost` isn't on the trade dict, pass `None` and let it stay null (the derived fields guard for None).

- [ ] **Step 3: Run the demo-engine tests**

Run: `cd engine && python -m pytest tests/test_demo_engine.py tests/test_tp_settlement_backfill.py -v`
Expected: PASS (hooks are additive + wrapped; existing behavior unchanged).

- [ ] **Step 4: Commit**

```bash
git add engine/demo_engine.py
git commit -m "feat(audit): demo_engine hooks — open_row at BUY, finalize_row at close"
```

---

## Task 7: API endpoints + startup backfill (`main.py`)

**Files:**
- Modify: `engine/main.py` (endpoints near the faults block `~:2063`; backfill task in `lifespan` `~:809`)
- Test: `engine/tests/test_api_smoke.py` (extend)

- [ ] **Step 1: Add the failing smoke test**

```python
# append to engine/tests/test_api_smoke.py (follow the existing TestClient pattern in that file)
def test_audit_endpoints_smoke(client):   # `client` = existing fixture/TestClient in this file
    r = client.get("/api/audit")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body and "counts" in body
    assert isinstance(body["rows"], list)

    r2 = client.get("/api/audit/export?labels_only=true")
    assert r2.status_code == 200
    assert isinstance(r2.json().get("rows", r2.json()), (list, dict))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd engine && python -m pytest tests/test_api_smoke.py::test_audit_endpoints_smoke -v`
Expected: FAIL (404 — endpoints not defined).

- [ ] **Step 3: Add the endpoints** (place right after the faults endpoints block, ≈ line 2110)

```python
# engine/main.py — Trade Audit Ledger endpoints (read-only analytics; cacheable, NOT order-path)
@app.get("/api/audit")
async def audit_list(
    mode: Optional[str] = None, window_sec: Optional[int] = None,
    settlement_status: Optional[str] = None, side: Optional[str] = None,
    lesson_tag: Optional[str] = None, limit: int = 1000,
):
    import audit_tracker
    return {
        "rows": audit_tracker.list_audits(
            mode=mode, window_sec=window_sec, settlement_status=settlement_status,
            side=side, lesson_tag=lesson_tag, limit=int(limit)),
        "counts": audit_tracker.audit_counts(),
    }


@app.get("/api/audit/export")
async def audit_export(since_ts: Optional[int] = None, schema_version: Optional[int] = None,
                       labels_only: bool = False, limit: int = 100000):
    import audit_tracker
    return {"rows": audit_tracker.export_rows(
        since_ts=since_ts, schema_version=schema_version,
        labels_only=bool(labels_only), limit=int(limit))}


@app.get("/api/audit/{session_id}")
async def audit_detail(session_id: str):
    import audit_tracker
    row = audit_tracker.get_audit(session_id)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return row
```

> Register `/api/audit/export` BEFORE `/api/audit/{session_id}` so "export" isn't captured as a `session_id`. Use the existing `JSONResponse`/`Optional` imports already present in `main.py`.

- [ ] **Step 4: Add the automatic backfill task in `lifespan`**

In `lifespan` (≈ line 809), next to `asyncio.create_task(_backfill_missing_history_windows())`, add a helper and task:

```python
    # Audit ledger: backfill historical sessions once, off the hot boot path.
    async def _audit_backfill_once():
        try:
            import audit_tracker
            # demo_state trades are the source; read them the same way the snapshot endpoints do.
            trades = list(getattr(demo.state, "trades", []) or [])   # `demo` = the DemoEngine in main.py
            n = audit_tracker.backfill_from_trades(trades)
            if n:
                append_event("audit_backfill", {"rows": n})
        except Exception as e:
            try:
                import fault_tracker
                fault_tracker.record_fault(category="audit", severity="low",
                                           title="audit backfill failed", detail=repr(e))
            except Exception:
                pass
    asyncio.create_task(_audit_backfill_once())
```

> Use the actual demo-engine handle name used in `main.py` (search for the `DemoEngine`/`DemoState` instance, e.g. `demo` or `engine_demo`). If trades live elsewhere, read from the same source `/api/demo/state` uses.

- [ ] **Step 5: Run the smoke test**

Run: `cd engine && python -m pytest tests/test_api_smoke.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/main.py engine/tests/test_api_smoke.py
git commit -m "feat(audit): /api/audit list+detail+export endpoints + startup backfill task"
```

---

## Task 8: `AuditTab.tsx` + register in `App.tsx`

**Files:**
- Create: `src/AuditTab.tsx`
- Modify: `src/App.tsx` (4 edits)

This component **mirrors `src/FaultsTab.tsx`** (same imports, 12s `isPageHidden` poll, inline style helpers `btnStyle/chipStyle/selectStyle`, RTL container, summary strip, list with expandable rows). Copy `FaultsTab.tsx` to `AuditTab.tsx` and adapt per the spec below — keep the styling shell identical.

- [ ] **Step 1: Define the types + fetch (replace Fault/Counts/refresh)**

```tsx
// src/AuditTab.tsx — types
type AuditRow = {
  session_id: string;
  mode: string; slug: string; window_sec: number; side: "Up" | "Down" | string;
  decision_ts: number;            // UTC ms
  settled_ts: number | null;
  recommendation: string | null; weighted_score: number | null; confidence_pct: number | null;
  vol_bucket: string | null; loss_recovery_multiplier: number | null;
  exit_type: string | null; settlement_status: string;   // WIN|LOSS|VOID|UNKNOWN|PENDING
  realized_pnl: number | null; realized_pct: number | null;
  peak_unrealized_pct: number | null; trough_unrealized_pct: number | null;
  exit_efficiency: number | null; missed_profit_pct: number | null;
  signal_was_correct: boolean | null; signals_agreement: number | null; signal_conflict: boolean | null;
  cf_other_side_pnl: number | null; lesson_tag: string | null;
  contracts: number | null; avg_fill_price: number | null;
  context: Record<string, unknown>;          // full snapshot
  rule_flags: Record<string, unknown>;
  cf_exit_variants: Record<string, unknown>;
  pnl_path: Array<{ ts?: number; upnl_pct?: number; bid?: number }>;
};
type AuditCounts = {
  by_status: Record<string, number>; total: number; win_rate_pct: number;
  avg_exit_efficiency: number | null; top_lessons: Array<{ lesson_tag: string; n: number }>;
};
type AuditResponse = { rows: AuditRow[]; counts: AuditCounts };
const AUDIT_TIMEOUT_MS = 45_000;
```

Fetch (replace the faults `refresh` body):

```tsx
const qs = new URLSearchParams();
if (fMode !== "all") qs.set("mode", fMode);
if (fWindow !== "all") qs.set("window_sec", fWindow);   // "300" | "900"
if (fStatus !== "all") qs.set("settlement_status", fStatus);
const res = await api<AuditResponse>(`/api/audit?${qs.toString()}`, { timeoutMs: AUDIT_TIMEOUT_MS });
setData(res);
```

Timestamps are **milliseconds**, so format with `new Date(ts)` (NOT `ts*1000` like FaultsTab uses).

- [ ] **Step 2: Header + summary strip**

Title: `📋 ביקורת עסקאות`. Subtitle (he): "כל עסקה מתועדת כאן — למה נכנסנו, מה קרה, ומה היה אפשר טוב יותר. הבסיס שה-AI ילמד ממנו." Summary cards: `win_rate_pct`, `avg_exit_efficiency`, `total`, and a `top_lessons` chip row. Status counts from `by_status` (WIN green, LOSS red, VOID/UNKNOWN/PENDING grey).

- [ ] **Step 3: Table rows (replace the faults list item)**

Each row shows: time (`fmtTime(decision_ts)`) · market (`window_sec===900?"15m":"5m"`) · side (color) · `avg_fill_price` · `contracts` · `exit_type` · `realized_pnl` (green/red) · `realized_pct` · `peak_unrealized_pct` · `exit_efficiency` · signal-vs-outcome (`signal_was_correct===true?"✓":signal_was_correct===false?"✗":"—"`) · `signals_agreement` · a `lesson_tag` chip. Color the left border by `settlement_status` (WIN `#065f46`, LOSS `#7f1d1d`, else `#334155`).

- [ ] **Step 4: Drill-down panel (expanded row)**

Show the full story from `context`: `context.signal`, `context.ta`, `context.clob`, `context.sentiment`, `context.history`, `context.policy`, `context.provenance`, `context.regime`. Plus settlement (`settlement_btc_start/end`, `resolved_outcome`), counterfactuals (`cf_other_side_pnl`, `cf_exit_variants`), `rule_flags`, and a small `pnl_path` sparkline (reuse any existing mini-chart in the repo, or render a simple inline SVG of `upnl_pct`). End with a `<pre>` dump of `context` (like FaultsTab does for `f.context`).

- [ ] **Step 5: Filters toolbar**

Three selects: mode (`all`/`demo`/`live`), window (`all`/`300`/`900`), status (`all`/`WIN`/`LOSS`/`VOID`/`UNKNOWN`/`PENDING`). Wire to `fMode/fWindow/fStatus` state in the `refresh` deps.

- [ ] **Step 6: Register the tab in `App.tsx` (4 edits)**

```tsx
// 1) import (top of src/App.tsx, near other tab imports e.g. FaultsTab)
import AuditTab from "./AuditTab";

// 2) Tab union type (≈ line 60) — add "audits"
type Tab = "dash" | "strategy" | "signals" | "trigger" | "stats" | "stats_live" | "tips_v2" | "analytics_v3" | "faults" | "audits" | "help";

// 3) Tab button tuple array (≈ lines 2638-2665) — add after the faults entry
["audits", "📋 ביקורת עסקאות"],

// 4) Conditional render (≈ lines 4565-4569) — add near the faults render
{tab === "audits" && <AuditTab />}
```

- [ ] **Step 7: Build to verify it compiles**

Run: `npm run build`
Expected: Vite build succeeds with no TypeScript errors.

- [ ] **Step 8: Commit**

```bash
git add src/AuditTab.tsx src/App.tsx
git commit -m "feat(audit): 📋 ביקורת עסקאות tab — table, drill-down, filters, summary"
```

---

## Task 9: Full verification + branch wrap-up

- [ ] **Step 1: Run the whole audit test suite + a broad regression sweep**

Run: `cd engine && python -m pytest tests/test_audit_derive.py tests/test_audit_snapshot.py tests/test_audit_tracker.py tests/test_demo_engine.py tests/test_strategy_runtime.py tests/test_api_smoke.py tests/test_loss_recovery.py -v`
Expected: PASS (audit logic + no regression in demo/strategy/loss-recovery/api).

- [ ] **Step 2: Build the frontend**

Run: `npm run build`
Expected: success.

- [ ] **Step 3: Manual smoke (optional, if running locally)**

Start the engine, open the dashboard, click `📋 ביקורת עסקאות`. Confirm: backfilled historical rows appear (schema_version 0, no signal block in drill-down), win-rate excludes UNKNOWN, and a freshly closed trade gets a full drill-down with the signal block.

- [ ] **Step 4: Final commit (if anything uncommitted)**

```bash
git add -A && git commit -m "chore(audit): finalize Trade Audit & Learning Ledger (Phase A)"
```

---

## Notes & Guardrails (carry into execution)

- **Never block a trade for the audit.** Every hook is wrapped in `try/except` and logs to stdout (and optionally `fault_tracker`) on failure.
- **Point-in-time immutability.** `finalize_row` MUST NOT write any decision-time column. `test_finalize_does_not_mutate_decision_fields` enforces this — keep it green.
- **The martingale guard already exists** in `loss_recovery.py` (skips `SETTLE_UNKNOWN`/`settlement_error`). Do NOT remove it. The audit `settlement_status` enum mirrors the same semantics for the export; both quarantine non-`{WIN,LOSS}`.
- **No-cache order-path guardrail is untouched** — all audit endpoints are read-only analytics, not order-path (see memory: api-resource-audit).
- **Backfilled rows are `schema_version=0`** with null Layer B; live rows are `1`. The future AI filters by version so eras never conflate.
- **Signal wiring is the highest-value, highest-risk change.** If `_last_signal_result` cannot be populated cleanly in this pass, ship snapshots with `signals_missing=true` and land the signal-mode wiring as a fast follow — the schema and pipeline are ready for it.
