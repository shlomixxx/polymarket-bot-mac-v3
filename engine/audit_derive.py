"""Pure, I/O-free derivations for the Trade Audit Ledger.

Every function is total (returns None on missing/invalid input) and never raises,
so it is safe to call from the trading loop and to recompute offline. It reads ONLY
the immutable decision snapshot + the outcome dict — never live data.
"""
from __future__ import annotations

from typing import Any, Optional

LABEL_OK = ("WIN", "LOSS")


def settlement_status(outcome: dict[str, Any]) -> str:
    """Map a closing trade/outcome to the canonical enum, separate from numeric PnL."""
    typ = str(outcome.get("type") or "")
    # Order matters: a real SETTLE_UNKNOWN also carries voided=True in the engine, but it is an
    # UNKNOWN (couldn't determine the outcome), not a deliberate VOID/refund — classify it first.
    if typ == "SETTLE_UNKNOWN" or outcome.get("settlement_error"):
        return "UNKNOWN"
    if outcome.get("voided"):
        return "VOID"
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
    """Fraction of the achievable favorable move captured at exit, bounded to [0, 1].

    Denominator is max(peak_pct, realized_pct): a held-to-settlement win whose realized %
    exceeds the intraday bid-peak reads as ~1.0 (optimal capture) instead of >1, so the
    average stays interpretable. None for losses (realized_pct<0) or when there was no
    upside at all (max ≤ 0).
    """
    try:
        if realized_pct is None or peak_pct is None or float(realized_pct) < 0:
            return None
        denom = max(float(peak_pct), float(realized_pct))
        if denom <= 0:
            return None
        return round(float(realized_pct) / denom, 4)
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
    """Counterfactual: PnL if we'd taken the OPPOSITE leg, same contracts, at its entry ask."""
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
    """Per-component directional lean in {-1,0,1} (positive = Up).

    compute_signals' ta/sentiment sub-dicts use the key "score"; we also accept the explicit
    "ta_score"/"sentiment_score" names so the function is robust to either source.
    """
    ta_d = snapshot.get("ta") or {}
    clob_d = snapshot.get("clob") or {}
    sent_d = snapshot.get("sentiment") or {}
    ta = ta_d.get("ta_score") if ta_d.get("ta_score") is not None else ta_d.get("score")
    clob = clob_d.get("net_score")
    sent = sent_d.get("sentiment_score") if sent_d.get("sentiment_score") is not None else sent_d.get("score")

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
        "outcome_reason": outcome.get("outcome_reason"),
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
