"""Assemble the immutable point-in-time decision snapshot (schema_version 1).

Pure dict assembly. The snapshot mirrors the design spec and is the value written
ONCE at the entry tick (never recomputed later). `signal_result` is the dict
returned by signal_engine.compute_signals() (it nests components under "sub").
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

_GIT_SHA: Optional[str] = None


def get_git_sha() -> str:
    global _GIT_SHA
    if _GIT_SHA is None:
        # Prod (Railway /data) has no .git dir, so `git rev-parse` returns "". Railway injects
        # the build commit as RAILWAY_GIT_COMMIT_SHA — prefer it so prod rows carry a code_version.
        _railway = os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        if _railway:
            _GIT_SHA = _railway.strip()[:12]
        else:
            try:
                _GIT_SHA = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).resolve().parent),
                    stderr=subprocess.DEVNULL, timeout=2).decode().strip()
            except Exception:
                _GIT_SHA = ""
    return _GIT_SHA


def build_decision_snapshot(
    *, mode: str, side: str, slug: str, epoch: int, window_sec: int,
    decision_ts_ms: int, code_version: str,
    signal_result: Optional[dict[str, Any]],
    policy: dict[str, Any], book: dict[str, Any], provenance: dict[str, Any],
    regime: dict[str, Any], execution: dict[str, Any],
    btc_spot_at_entry: Optional[float],
    **extra: Any,
) -> dict[str, Any]:
    """Build the immutable decision snapshot.

    `**extra` is a forward-compatible catch-all: any NEW audit_inputs key (e.g. market,
    raw_book_up/down, funding_rate_pct, window_open_btc, spot_vs_open_pct added by the
    enrichment batch) is merged into the snapshot below so the pipeline never raises
    TypeError when callers stash additional context. Existing keys are NEVER clobbered.
    """
    sig = signal_result or {}
    sub = sig.get("sub") or {}
    signals_missing = signal_result is None

    prov = dict(provenance or {})
    prov.setdefault("signals_missing", signals_missing)
    prov.setdefault("signals_stale", False)

    snapshot = {
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
        "action_propensity": 1.0,
        "exploration_flag": False,
    }

    # Merge any forward-compatible extra audit_inputs keys WITHOUT clobbering the
    # fixed snapshot fields above. The new keys (market/raw_book_up/window_open_btc/...)
    # carry distinct names, so they land readably at the top level of the recorded context.
    if extra:
        for _k, _v in extra.items():
            snapshot.setdefault(_k, _v)

    return snapshot


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
