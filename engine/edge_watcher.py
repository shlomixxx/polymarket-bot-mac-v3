"""Edge-Watcher analysis orchestrator (recording-only / advisory).

Mirrors trade_coach.py: pure functions over a list of audit-row dicts (the
`light=False` shape from audit_tracker.export_rows). It scans the ledger and emits
ONE plain-Hebrew verdict (collecting -> watching -> forming -> confirmed) telling the
owner when a statistically genuine, tradeable edge has emerged.

INVARIANTS (load-bearing — see docs/superpowers/specs/2026-06-08-edge-watcher-design.md):
  * RECORDING-ONLY: imports NOTHING from trading code (no demo_engine / strategy_runner /
    runner / order path). It never writes audit_rows. The only writes (in later tasks) are
    to the private edge_state sidecar DB.
  * NEVER RAISES: every public fn returns a safe value on malformed / empty input, never an
    exception (mirror trade_coach.compute_lessons' defensive style).
  * OFF THE EVENT LOOP: reached only via the cached endpoint on a worker thread.
  * The ADVERSARIAL STAT-FIXES are load-bearing — implement the FIXED versions from the spec.

This task (Task 2) ships only: the constants block + the four row extractors
(y_tp / y_dir / r_net / clean) + the `_num` numeric helper. Later tasks add the
bucketizer, slice evaluator, persistence wiring and the detect_edges orchestrator.
"""

from __future__ import annotations

import math
from typing import Any, Optional


# ── Constants: single source of truth (spec §3.1, §7) ───────────────────────
TP_PCT = 18.0
REAL_RATE = 0.035          # real Polymarket round-trip wedge
DEMO_FEE_RATE = 0.002      # already booked in the ledger (per side)
STAKE_USD = 5.0
TOTAL_MIN = 800            # below -> "collecting"
N_SLICE_MIN_EFFECTIVE = 400  # effective (design-effect-adjusted) slice size
FIRE_RATE_MIN = 0.05
MIN_RAW_LIFT_PTS = 5.0
ECON_MIN_NET = 0.10        # +$ per unit stake (master gate)
ECON_ABSTAIN_NET = -0.10   # E: genuinely costly
BH_Q = 0.10
DSR_MIN = 0.95
MIN_CONFIRMATIONS = 3
CONFIRM_SPACING_TRADES = 100  # >= this many new settled trades between confirmations


# ── Defensive numeric coercion ──────────────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Never raises.

    Tolerates None, numeric strings, ints/floats. Rejects NaN/Inf, lists, dicts,
    booleans-as-numbers are accepted (bool is an int) but callers don't rely on that.
    """
    if v is None or isinstance(v, (list, dict, tuple, set)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


# ── Row extractors (operate on ONE row dict; tolerate malformed rows) ───────
def y_tp(row: Any) -> int:
    """1 iff realized TP exit (single definition — spec I1).

    Does NOT blend in the peak>=18 near-miss counterfactual (that is a secondary
    diagnostic only). Any non-dict / missing field returns 0.
    """
    try:
        return 1 if (isinstance(row, dict) and row.get("exit_type") == "TP") else 0
    except Exception:
        return 0


def y_dir(row: Any) -> Optional[int]:
    """1 / 0 / None — directional held-to-resolution, demo-fee-netted.

    None unless the row resolved Up/Down AND a finite held-to-resolution
    counterfactual P&L is present (spec target A — diagnostic only).
    """
    try:
        if not isinstance(row, dict):
            return None
        cf = row.get("cf_exit_variants")
        if not isinstance(cf, dict):
            return None
        v = _num(cf.get("pnl_if_held_to_resolution"))
        if v is None or row.get("resolved_outcome") not in ("Up", "Down"):
            return None
        return 1 if v > 0 else 0
    except Exception:
        return None


def r_net(row: Any) -> Optional[float]:
    """Stake-normalized, real-fee net $ per unit stake (spec §3.1, fixes I3/I6).

    The ledger's realized_pnl is netted at the DEMO fee (DEMO_FEE_RATE per side), not
    the real ~3-4% Polymarket round-trip. Under martingale, stakes also vary. So:

        r_net = (realized_pnl - wedge) / max(loss_recovery_multiplier, 1.0)

    where `wedge` uses the row's ACTUAL fill_price * contracts when both are present
    (a flat per-$5 wedge is biased optimistic on cheap long-shot fills — exactly where
    the TP mechanic lives). Falls back to a flat STAKE_USD wedge otherwise.
    Returns None if realized_pnl is missing/unparseable. Never raises.
    """
    try:
        if not isinstance(row, dict):
            return None
        rp = _num(row.get("realized_pnl"))
        if rp is None:
            return None
        fill = _num(row.get("fill_price"))
        contracts = _num(row.get("contracts"))
        real_wedge_rate = REAL_RATE - 2 * DEMO_FEE_RATE
        if fill and contracts:
            wedge = real_wedge_rate * fill * contracts
        else:
            wedge = real_wedge_rate * STAKE_USD
        mult = max(_num(row.get("loss_recovery_multiplier")) or 1.0, 1.0)
        return (rp - wedge) / mult
    except Exception:
        return None


def clean(row: Any) -> bool:
    """Martingale / exploration confound filter (spec G5).

    True only for rows that are NOT under loss-recovery and NOT exploration:
    recovery_active is not True, loss_recovery_multiplier <= 1.0, exploration_flag
    in (0, False, None). Non-dict rows are NOT clean. Never raises.
    """
    try:
        if not isinstance(row, dict):
            return False
        rf = row.get("rule_flags")
        if not isinstance(rf, dict):
            rf = {}
        return (
            rf.get("recovery_active") is not True
            and (_num(row.get("loss_recovery_multiplier")) or 1.0) <= 1.0
            and row.get("exploration_flag") in (0, False, None)
        )
    except Exception:
        return False
