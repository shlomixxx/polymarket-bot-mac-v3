"""
Tests for the revived CLOB-imbalance feed: WS book DEPTH cached on TokenPrice and
served by PriceStreamManager.get_book() into analyze_clob_imbalance (audit-only).

WHY: compute_signals() in the strategy loop was called WITHOUT books, so the
0.30-weight CLOB sub-signal was permanently available=False. We now reuse the
order-book depth that already arrives on the existing WS (no new network) and
expose it via get_book(), with FRESHNESS so a stale book degrades to None (never
bad data) rather than feeding analyze_clob_imbalance garbage.
"""
import time

from clob_imbalance import analyze_clob_imbalance
from ws_price_stream import (
    BOOK_DEPTH_LEVELS,
    PriceStreamManager,
    TokenPrice,
    price_stream,
)


def _make_book_msg(n_bids: int, n_asks: int) -> dict:
    """A Polymarket-style book/initial payload: levels are {"price","size"} strings."""
    # Deliberately UNSORTED to prove update_from_book sorts (bids desc, asks asc).
    bids = [{"price": f"{0.40 + 0.01 * i:.2f}", "size": f"{10 + i}"} for i in range(n_bids)]
    asks = [{"price": f"{0.60 - 0.01 * i:.2f}", "size": f"{5 + i}"} for i in range(n_asks)]
    # shuffle order a bit
    bids = bids[::-1]
    return {"event_type": "book", "asset_id": "tok", "bids": bids, "asks": asks}


def test_update_from_book_populates_capped_levels_in_expected_shape():
    tp = TokenPrice()
    # Send MORE than the cap to prove truncation to top-10.
    msg = _make_book_msg(n_bids=25, n_asks=25)
    changed = tp.update_from_book(msg)

    assert changed is True
    # Capped to <=10 levels each.
    assert len(tp.bids) == BOOK_DEPTH_LEVELS
    assert len(tp.asks) == BOOK_DEPTH_LEVELS
    assert len(tp.bids) <= 10 and len(tp.asks) <= 10

    # Shape exactly what compute_book_depth reads: dicts with float price & size.
    for lvl in tp.bids + tp.asks:
        assert set(lvl.keys()) == {"price", "size"}
        assert isinstance(lvl["price"], float)
        assert isinstance(lvl["size"], float)

    # Sorted: bids descending, asks ascending (best at index 0).
    assert tp.bids[0]["price"] >= tp.bids[1]["price"]
    assert tp.asks[0]["price"] <= tp.asks[1]["price"]
    assert tp.book_ts > 0


def test_get_book_returns_book_when_fresh_and_none_when_stale():
    mgr = PriceStreamManager()
    tp = TokenPrice()
    tp.update_from_book(_make_book_msg(n_bids=5, n_asks=5))
    mgr._prices["tok"] = tp

    # Fresh -> returns the book.
    book = mgr.get_book("tok", max_age_sec=30.0)
    assert book is not None
    assert "bids" in book and "asks" in book
    assert len(book["bids"]) == 5 and len(book["asks"]) == 5

    # Returned lists are copies -> mutating them must not corrupt the cache.
    book["bids"].clear()
    assert len(mgr._prices["tok"].bids) == 5

    # Inject an OLD book_ts -> stale -> None (never feed stale depth).
    tp.book_ts = time.time() - 120.0
    assert mgr.get_book("tok", max_age_sec=30.0) is None

    # Unknown token -> None.
    assert mgr.get_book("does-not-exist", max_age_sec=30.0) is None

    # No depth ever cached -> None even if price ts is fresh.
    empty = TokenPrice()
    empty.ts = time.time()
    mgr._prices["empty"] = empty
    assert mgr.get_book("empty", max_age_sec=30.0) is None


def test_get_book_output_feeds_analyze_clob_imbalance_available_true():
    """KEY correctness check: the get_book() shape is EXACTLY what
    analyze_clob_imbalance expects -> available=True end to end, no network."""
    mgr = PriceStreamManager()

    up = TokenPrice()
    # Up: heavy bid depth -> imbalance toward bids.
    up.update_from_book({
        "event_type": "book", "asset_id": "up",
        "bids": [{"price": "0.55", "size": "1000"}, {"price": "0.54", "size": "800"}],
        "asks": [{"price": "0.57", "size": "10"}],
    })
    down = TokenPrice()
    # Down: heavy ask depth -> imbalance toward asks.
    down.update_from_book({
        "event_type": "book", "asset_id": "down",
        "bids": [{"price": "0.43", "size": "10"}],
        "asks": [{"price": "0.45", "size": "900"}, {"price": "0.46", "size": "700"}],
    })
    mgr._prices["up"] = up
    mgr._prices["down"] = down

    up_book = mgr.get_book("up", max_age_sec=30.0)
    down_book = mgr.get_book("down", max_age_sec=30.0)
    assert up_book is not None and down_book is not None

    result = analyze_clob_imbalance(up_book, down_book)
    assert result["available"] is True
    # Up has the stronger bid imbalance -> net_score positive -> signal "up".
    assert result["net_score"] > 0
    assert result["signal"] == "up"
    assert result["up"]["bid_depth"] > result["up"]["ask_depth"]


def test_stale_book_to_none_keeps_clob_unavailable():
    """A stale book degrades to None, and analyze_clob_imbalance(None, None) is
    available=False — proving no bad/stale data ever reaches the signal."""
    mgr = PriceStreamManager()
    tp = TokenPrice()
    tp.update_from_book(_make_book_msg(3, 3))
    tp.book_ts = time.time() - 999.0  # very stale
    mgr._prices["tok"] = tp

    assert mgr.get_book("tok", max_age_sec=30.0) is None
    assert analyze_clob_imbalance(None, None)["available"] is False


def test_module_singleton_exposes_get_book():
    # The strategy loop accesses the in-process singleton `price_stream`.
    assert hasattr(price_stream, "get_book")
