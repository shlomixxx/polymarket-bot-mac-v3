"""
ניתוח עומק ספר הפקודות (CLOB Imbalance) לצד Up ו-Down.
צד עם bid depth גבוה יותר נחשב "מועדף" על-ידי smart money.
"""
from __future__ import annotations

import time
from typing import Any, Optional


def compute_book_depth(book: Optional[dict[str, Any]], top_n: int = 10) -> dict[str, Any]:
    """
    מחשב עומק bid/ask של ספר הפקודות (N רמות עליות).
    מחזיר bid_depth, ask_depth, spread (בדולרים).
    """
    if not book:
        return {"bid_depth": 0.0, "ask_depth": 0.0, "spread": None}

    bids = book.get("bids") or []
    asks = book.get("asks") or []

    # סה"כ דולר בN רמות עליות: size * price
    bid_depth = sum(
        float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:top_n]
    )
    ask_depth = sum(
        float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:top_n]
    )

    spread: Optional[float] = None
    if bids and asks:
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

    return {"bid_depth": bid_depth, "ask_depth": ask_depth, "spread": spread}


def compute_imbalance_score(bid_depth: float, ask_depth: float) -> float:
    """
    ציון imbalance בין -1 ל-+1.
    +1 = ביד כולו, -1 = אסק כולו, 0 = שיווי משקל.
    """
    total = bid_depth + ask_depth
    if total <= 0:
        return 0.0
    return (bid_depth - ask_depth) / total


# ─────────────────────────────────────────────────────────────────────────────
# RECORDING-ONLY market-microstructure helpers.
#
# DATA-ONLY: these are computed from a SINGLE book snapshot and stamped into the
# audit ledger (via analyze_clob_imbalance's per-side sub-dicts) for a future AI.
# They DO NOT feed net_score / the trading signal / the entry side. All guard for
# missing/empty/zero-size levels by returning None — they MUST never raise.
# ─────────────────────────────────────────────────────────────────────────────
def _top_level(book: Optional[dict[str, Any]]) -> tuple[Optional[dict], Optional[dict]]:
    """Best (top-of-book) bid/ask level dicts, or (None, None) if missing."""
    if not book:
        return None, None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = bids[0] if bids else None
    best_ask = asks[0] if asks else None
    return best_bid, best_ask


def compute_microprice(book: Optional[dict[str, Any]]) -> Optional[float]:
    """
    Size-weighted fair value from the top of book (note the CROSS-weighting):
        (best_bid*ask_size + best_ask*bid_size) / (bid_size + ask_size)
    The larger side pulls the fair value AWAY from it (more size on the bid means
    fewer sellers there -> fair value drifts up toward the ask). None if either
    side is missing or both top sizes are 0.
    """
    try:
        best_bid, best_ask = _top_level(book)
        if not best_bid or not best_ask:
            return None
        bid_px = float(best_bid.get("price", 0))
        ask_px = float(best_ask.get("price", 0))
        bid_sz = float(best_bid.get("size", 0))
        ask_sz = float(best_ask.get("size", 0))
        total = bid_sz + ask_sz
        if total <= 0:
            return None
        return (bid_px * ask_sz + ask_px * bid_sz) / total
    except (TypeError, ValueError, KeyError):
        return None


def compute_l1_imbalance(book: Optional[dict[str, Any]]) -> Optional[float]:
    """
    Top-of-book size imbalance in [-1, +1]:
        (bid_size - ask_size) / (bid_size + ask_size)
    +1 = all top size on the bid, -1 = all on the ask. None if a side is missing
    or both top sizes are 0.
    """
    try:
        best_bid, best_ask = _top_level(book)
        if not best_bid or not best_ask:
            return None
        bid_sz = float(best_bid.get("size", 0))
        ask_sz = float(best_ask.get("size", 0))
        total = bid_sz + ask_sz
        if total <= 0:
            return None
        return (bid_sz - ask_sz) / total
    except (TypeError, ValueError, KeyError):
        return None


def compute_spread(
    book: Optional[dict[str, Any]],
) -> tuple[Optional[float], Optional[float]]:
    """
    (spread, spread_pct) from the top of book:
        spread     = best_ask - best_bid
        spread_pct = spread / mid * 100   (None when mid == 0)
    (None, None) if either side is missing.
    """
    try:
        best_bid, best_ask = _top_level(book)
        if not best_bid or not best_ask:
            return None, None
        bid_px = float(best_bid.get("price", 0))
        ask_px = float(best_ask.get("price", 0))
        spread = ask_px - bid_px
        mid = (ask_px + bid_px) / 2.0
        spread_pct = (spread / mid * 100.0) if mid != 0 else None
        return spread, spread_pct
    except (TypeError, ValueError, KeyError):
        return None, None


def compute_depth_ratio(
    book: Optional[dict[str, Any]], levels: int = 5
) -> Optional[float]:
    """
    Book "slope": total bid size vs total ask size over the top ~N levels:
        (sum_bid_size - sum_ask_size) / (sum_bid_size + sum_ask_size)   in [-1, +1]
    None if both totals are 0 / book missing.
    """
    try:
        if not book:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        sum_bid = sum(float(b.get("size", 0)) for b in bids[:levels])
        sum_ask = sum(float(a.get("size", 0)) for a in asks[:levels])
        total = sum_bid + sum_ask
        if total <= 0:
            return None
        return (sum_bid - sum_ask) / total
    except (TypeError, ValueError, KeyError):
        return None


def microstructure_for_book(
    book: Optional[dict[str, Any]], levels: int = 5
) -> dict[str, Any]:
    """
    Bundle all recording-only microstructure features for ONE side's book.
    Every field is None-safe; this never raises.
    """
    micro = compute_microprice(book)
    l1 = compute_l1_imbalance(book)
    spread, spread_pct = compute_spread(book)
    depth_ratio = compute_depth_ratio(book, levels=levels)
    return {
        "microprice": round(micro, 6) if micro is not None else None,
        "l1_imbalance": round(l1, 6) if l1 is not None else None,
        "spread": round(spread, 6) if spread is not None else None,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "depth_ratio": round(depth_ratio, 6) if depth_ratio is not None else None,
    }


def market_features(
    up_ask: Optional[float],
    down_ask: Optional[float],
    model_up_prob: Optional[float],
) -> dict[str, Any]:
    """
    RECORDING-ONLY prediction-market features. The Polymarket share asks ARE the
    market's implied probabilities. Computed at DECISION TIME (the edge cannot be
    reconstructed later). None-safe; never raises. Does NOT affect the trade.

      vig              = up_ask + down_ask - 1                  (overround)
      up_implied_prob  = up_ask / (up_ask + down_ask)           (normalized prob)
      down_implied_prob= down_ask / (up_ask + down_ask)
      ta_vs_market_edge_up = model_up_prob - up_implied_prob    (model vs market)
    """
    out: dict[str, Any] = {
        "up_ask": None,
        "down_ask": None,
        "vig": None,
        "up_implied_prob": None,
        "down_implied_prob": None,
        "ta_vs_market_edge_up": None,
    }
    try:
        u = float(up_ask) if up_ask is not None else None
        d = float(down_ask) if down_ask is not None else None
        out["up_ask"] = u
        out["down_ask"] = d
        if u is None or d is None:
            return out
        out["vig"] = round(u + d - 1.0, 6)
        denom = u + d
        if denom > 0:
            up_p = u / denom
            down_p = d / denom
            out["up_implied_prob"] = round(up_p, 6)
            out["down_implied_prob"] = round(down_p, 6)
            if model_up_prob is not None:
                out["ta_vs_market_edge_up"] = round(float(model_up_prob) - up_p, 6)
        return out
    except (TypeError, ValueError):
        return out


def analyze_clob_imbalance(
    up_book: Optional[dict[str, Any]],
    down_book: Optional[dict[str, Any]],
    top_n: int = 10,
) -> dict[str, Any]:
    """
    משווה עומק ספר הפקודות של Up ו-Down.
    מחזיר ניתוח עם המלצת כיוון לפי הטיית smart money.
    """
    if not up_book and not down_book:
        return {"available": False, "error": "no books provided"}

    up_depth = compute_book_depth(up_book, top_n)
    down_depth = compute_book_depth(down_book, top_n)

    up_imbalance = compute_imbalance_score(up_depth["bid_depth"], up_depth["ask_depth"])
    down_imbalance = compute_imbalance_score(down_depth["bid_depth"], down_depth["ask_depth"])

    # net_score: חיובי = Up מועדף, שלילי = Down מועדף
    net_score = up_imbalance - down_imbalance

    signal = "neutral"
    if net_score > 0.05:
        signal = "up"
    elif net_score < -0.05:
        signal = "down"

    if signal == "up":
        note = (
            f"Up bid depth חזק יותר "
            f"(Up: {up_imbalance:+.2f}, Down: {down_imbalance:+.2f})"
        )
    elif signal == "down":
        note = (
            f"Down bid depth חזק יותר "
            f"(Down: {down_imbalance:+.2f}, Up: {up_imbalance:+.2f})"
        )
    else:
        note = f"CLOB מאוזן (Up: {up_imbalance:+.2f}, Down: {down_imbalance:+.2f})"

    signals = [{"name": "CLOB", "signal": signal, "note": note}]

    # RECORDING-ONLY microstructure per side (microprice / L1-imbalance / spread /
    # spread_pct / depth-ratio). Additive — NOT used for net_score / signal above.
    # None-safe; never raises (guarded inside microstructure_for_book).
    up_micro = microstructure_for_book(up_book, levels=top_n)
    down_micro = microstructure_for_book(down_book, levels=top_n)

    return {
        "available": True,
        "up": {
            "bid_depth": round(up_depth["bid_depth"], 2),
            "ask_depth": round(up_depth["ask_depth"], 2),
            "spread": round(up_depth["spread"], 4) if up_depth["spread"] is not None else None,
            "imbalance": round(up_imbalance, 4),
            # recording-only microstructure (does not affect net_score / signal):
            "microprice": up_micro["microprice"],
            "l1_imbalance": up_micro["l1_imbalance"],
            "spread_pct": up_micro["spread_pct"],
            "depth_ratio": up_micro["depth_ratio"],
        },
        "down": {
            "bid_depth": round(down_depth["bid_depth"], 2),
            "ask_depth": round(down_depth["ask_depth"], 2),
            "spread": round(down_depth["spread"], 4) if down_depth["spread"] is not None else None,
            "imbalance": round(down_imbalance, 4),
            # recording-only microstructure (does not affect net_score / signal):
            "microprice": down_micro["microprice"],
            "l1_imbalance": down_micro["l1_imbalance"],
            "spread_pct": down_micro["spread_pct"],
            "depth_ratio": down_micro["depth_ratio"],
        },
        "net_score": round(net_score, 4),
        "signal": signal,
        "signals": signals,
        "ts": time.time(),
    }
