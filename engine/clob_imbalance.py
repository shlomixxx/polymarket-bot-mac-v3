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

    return {
        "available": True,
        "up": {
            "bid_depth": round(up_depth["bid_depth"], 2),
            "ask_depth": round(up_depth["ask_depth"], 2),
            "spread": round(up_depth["spread"], 4) if up_depth["spread"] is not None else None,
            "imbalance": round(up_imbalance, 4),
        },
        "down": {
            "bid_depth": round(down_depth["bid_depth"], 2),
            "ask_depth": round(down_depth["ask_depth"], 2),
            "spread": round(down_depth["spread"], 4) if down_depth["spread"] is not None else None,
            "imbalance": round(down_imbalance, 4),
        },
        "net_score": round(net_score, 4),
        "signal": signal,
        "signals": signals,
        "ts": time.time(),
    }
