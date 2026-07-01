"""
Tests for the Chainlink Data Stream price feed (chainlink_price_stream.py).

This is the SAME feed Polymarket resolves BTC "Up or Down 5m" markets on:
wss://ws-live-data.polymarket.com, topic crypto_prices_chainlink. Empirically the
feed answers a `subscribe` with a one-shot ~60s rolling snapshot of 1Hz ticks
(payload.data = [{timestamp<ms>, value}, ...]); the manager re-subscribes to poll.

The logic under test (buffer dedup, current-price freshness, Price-to-Beat boundary
selection with a cold-start guard, and the immutable per-window cache) is pure and
network-free.
"""
import time

from chainlink_price_stream import (
    ChainlinkPriceStream,
    TickBuffer,
    PTB_BOUNDARY_GRACE_SEC,
    chainlink_stream,
)


def _snapshot(start_sec: int, count: int, base: float = 58000.0, step: float = 1.0) -> dict:
    """A crypto_prices_chainlink frame: 1Hz ticks starting at start_sec (whole seconds)."""
    data = [
        {"timestamp": (start_sec + i) * 1000, "value": base + i * step}
        for i in range(count)
    ]
    return {"topic": "crypto_prices_chainlink", "type": "subscribe",
            "timestamp": (start_sec + count) * 1000,
            "payload": {"symbol": "btc/usd", "data": data}}


# ── TickBuffer: ingest / dedup / prune ─────────────────────────────────────────

def test_ingest_dedups_by_timestamp_and_keeps_latest_value():
    buf = TickBuffer(max_ticks=100)
    buf.ingest([{"timestamp": 1000, "value": 58010.0}, {"timestamp": 2000, "value": 58020.0}])
    # Overlapping re-subscribe snapshot: 2000 repeats, 3000 is new.
    buf.ingest([{"timestamp": 2000, "value": 58020.0}, {"timestamp": 3000, "value": 58030.0}])
    assert buf.size() == 3
    assert buf.latest() == (3000, 58030.0)


def test_ingest_prunes_oldest_beyond_capacity():
    buf = TickBuffer(max_ticks=5)
    buf.ingest([{"timestamp": t * 1000, "value": 58000.0 + t} for t in range(1, 11)])  # 10 ticks
    assert buf.size() == 5
    # Oldest dropped; newest kept.
    assert buf.earliest_ts_ms() == 6000
    assert buf.latest() == (10000, 58010.0)


def test_ingest_ignores_malformed_entries():
    buf = TickBuffer()
    buf.ingest([
        {"timestamp": 1000, "value": 58000.0},
        {"timestamp": None, "value": 58005.0},
        {"value": 58007.0},                   # no timestamp
        {"timestamp": 2000},                  # no value
        {"timestamp": "bad", "value": "x"},   # non-numeric
        "not-a-dict",
    ])
    assert buf.size() == 1
    assert buf.latest() == (1000, 58000.0)


# ── Price to Beat: boundary selection + cold-start guard ───────────────────────

def test_ptb_returns_boundary_tick_when_coverage_starts_before_window():
    buf = TickBuffer()
    ws = 1_800_000_000  # window_start (multiple of 300 not required for logic)
    # coverage from 5s BEFORE the boundary through 10s after.
    buf.ingest([{"timestamp": (ws - 5 + i) * 1000, "value": 58000.0 + i} for i in range(16)])
    res = buf.price_to_beat(ws)
    assert res is not None
    boundary_ts, value = res
    assert boundary_ts == ws * 1000            # exact boundary second
    assert value == 58000.0 + 5                 # the tick AT window_start


def test_ptb_cold_start_midwindow_returns_none():
    """Joined mid-window: earliest tick is AFTER window_start -> we do NOT have the
    true boundary tick, so must return None (never a wrong number)."""
    buf = TickBuffer()
    ws = 1_800_000_000
    # earliest tick is 120s AFTER window_start.
    buf.ingest([{"timestamp": (ws + 120 + i) * 1000, "value": 58000.0 + i} for i in range(60)])
    assert buf.price_to_beat(ws) is None


def test_ptb_none_when_boundary_tick_not_arrived_yet():
    buf = TickBuffer()
    ws = 1_800_000_000
    # all ticks strictly before window_start (window just started, boundary tick not in feed yet)
    buf.ingest([{"timestamp": (ws - 30 + i) * 1000, "value": 58000.0 + i} for i in range(30)])
    assert buf.price_to_beat(ws) is None


def test_ptb_tolerates_missing_exact_second_within_coverage():
    """Coverage starts before boundary but the exact window_start second is missing;
    Polymarket would take the next tick >= window_start, and so do we."""
    buf = TickBuffer()
    ws = 1_800_000_000
    ticks = []
    for i in range(-3, 8):
        if i == 0:
            continue  # drop the exact boundary second
        ticks.append({"timestamp": (ws + i) * 1000, "value": 58000.0 + i})
    buf.ingest(ticks)
    res = buf.price_to_beat(ws)
    assert res is not None
    boundary_ts, value = res
    assert boundary_ts == (ws + 1) * 1000       # first tick >= window_start
    assert value == 58000.0 + 1


def test_ptb_rejects_large_gap_at_boundary_beyond_grace():
    """Coverage exists before the boundary, but then a long gap swallows the whole
    grace window -> first tick >= window_start is too far out to trust -> None."""
    buf = TickBuffer()
    ws = 1_800_000_000
    grace = int(PTB_BOUNDARY_GRACE_SEC)
    buf.ingest([{"timestamp": (ws - 10 + i) * 1000, "value": 58000.0 + i} for i in range(10)])  # ends at ws-1
    # next tick only well past the grace window
    buf.ingest([{"timestamp": (ws + grace + 3) * 1000, "value": 58999.0}])
    assert buf.price_to_beat(ws) is None


# ── Manager: current price freshness ───────────────────────────────────────────

def test_get_current_price_fresh_and_stale():
    mgr = ChainlinkPriceStream()
    now = time.time()
    now_sec = int(now)
    mgr._ingest_message(_snapshot(now_sec - 2, 3, base=58500.0))  # newest tick ~now
    cur = mgr.get_current_price(max_age_sec=5.0)
    assert cur is not None
    assert cur["value"] == 58500.0 + 2
    assert cur["ts_ms"] == (now_sec) * 1000
    assert 0 <= cur["age_sec"] < 5.0

    # An old snapshot -> stale -> None (never serve a stale price as current).
    mgr2 = ChainlinkPriceStream()
    mgr2._ingest_message(_snapshot(now_sec - 100, 3))
    assert mgr2.get_current_price(max_age_sec=5.0) is None


def test_ingest_message_ignores_error_frames():
    mgr = ChainlinkPriceStream()
    assert mgr._ingest_message({"message": "Invalid request body", "connectionId": "x"}) is False
    assert mgr._ingest_message({"payload": {"symbol": "btc/usd"}}) is False  # no data list
    assert mgr.get_current_price() is None


# ── Manager: Price-to-Beat immutable per-window cache ──────────────────────────

def test_get_price_to_beat_caches_immutably_across_buffer_loss():
    mgr = ChainlinkPriceStream()
    ws = 1_800_000_400
    mgr._ingest_message(_snapshot(ws - 5, 20, base=70000.0))  # covers boundary
    v = mgr.get_price_to_beat(ws)
    assert v == 70000.0 + 5  # tick AT window_start

    # Simulate a reconnect that wipes the rolling buffer (only 60s backfill, no boundary).
    mgr._buffer = TickBuffer()
    mgr._ingest_message(_snapshot(ws + 200, 30))  # cold buffer, no boundary coverage
    # Fresh buffer alone could NOT recover it...
    assert mgr._buffer.price_to_beat(ws) is None
    # ...but the manager's immutable cache still returns the captured value.
    assert mgr.get_price_to_beat(ws) == 70000.0 + 5


def test_get_price_to_beat_none_on_cold_start():
    mgr = ChainlinkPriceStream()
    ws = 1_800_000_700
    mgr._ingest_message(_snapshot(ws + 120, 40))  # joined mid-window
    assert mgr.get_price_to_beat(ws) is None


# ── Robustness: reject non-finite / implausible values (review finding) ────────

def test_ingest_rejects_non_finite_and_implausible_values():
    """A garbage tick (NaN/Inf/0/negative/too-small) must NEVER enter the buffer —
    else it could be served as the EXACT current price / immutably-cached PTB."""
    buf = TickBuffer()
    buf.ingest([
        {"timestamp": 1000, "value": float("nan")},
        {"timestamp": 2000, "value": float("inf")},
        {"timestamp": 3000, "value": float("-inf")},
        {"timestamp": 4000, "value": 0.0},
        {"timestamp": 5000, "value": -5.0},
        {"timestamp": 6000, "value": 500.0},      # below BTC plausibility floor
        {"timestamp": 7000, "value": 58000.0},     # the only good tick
    ])
    assert buf.size() == 1
    assert buf.latest() == (7000, 58000.0)


def test_price_to_beat_never_returns_nan_from_garbage_boundary():
    buf = TickBuffer()
    ws = 1_800_000_000
    # boundary second carries NaN, but a good tick sits before + after
    buf.ingest([
        {"timestamp": (ws - 1) * 1000, "value": 58000.0},
        {"timestamp": ws * 1000, "value": float("nan")},
        {"timestamp": (ws + 1) * 1000, "value": 58001.0},
    ])
    res = buf.price_to_beat(ws)
    # NaN was dropped -> first trustworthy tick >= ws is ws+1
    assert res is not None
    assert res == ((ws + 1) * 1000, 58001.0)


# ── 15m windows: boundary tick must survive the whole window (review finding) ───

def test_buffer_holds_15m_window_boundary():
    buf = TickBuffer()  # default capacity must cover a 15m (900s) window + margin
    ws = 1_800_000_000
    # coverage from 5s before the boundary through the full 900s window
    buf.ingest([{"timestamp": (ws - 5 + i) * 1000, "value": 58000.0 + i} for i in range(5 + 900)])
    res = buf.price_to_beat(ws)
    assert res is not None
    assert res[0] == ws * 1000            # exact boundary tick still present, not pruned
    assert res[1] == 58000.0 + 5


# ── get_price_to_beat_full must not KeyError when the cache is full (finding) ───

def test_get_price_to_beat_full_no_keyerror_when_cache_full():
    from chainlink_price_stream import PTB_CACHE_MAX
    mgr = ChainlinkPriceStream()
    # Pre-fill the cache with MANY windows LARGER than our target window.
    big = 2_000_000_000
    for i in range(PTB_CACHE_MAX):
        mgr._ptb_cache[big + i * 300] = ((big + i * 300) * 1000, 60000.0)
    # Query an OLDER (smaller) window that has real buffer coverage.
    ws = 1_800_000_000
    mgr._ingest_message(_snapshot(ws - 5, 20, base=59000.0))
    full = mgr.get_price_to_beat_full(ws)   # must not raise KeyError
    assert full is not None
    assert full["value"] == 59000.0 + 5
    assert full["exact"] is True


# ── Freshness clock must track NEW ticks, not mere frame arrival (finding) ──────

def test_last_msg_ts_only_advances_on_new_ticks():
    import time as _t
    mgr = ChainlinkPriceStream()
    now = int(_t.time())
    assert mgr._ingest_message(_snapshot(now - 2, 3, base=58000.0)) is True
    ts1 = mgr._last_msg_ts
    assert ts1 > 0
    _t.sleep(0.02)
    # Re-ingest the IDENTICAL snapshot: 0 new ticks -> clock must NOT advance.
    assert mgr._ingest_message(_snapshot(now - 2, 3, base=58000.0)) is False
    assert mgr._last_msg_ts == ts1


# ── is_midwindow_gap: only true for a genuine cold-start join (finding) ─────────

def test_is_midwindow_gap_true_only_for_cold_start():
    ws = 1_800_000_000
    # (1) genuine cold-start: earliest tick AFTER window_start -> gap = True
    cold = ChainlinkPriceStream()
    cold._ingest_message(_snapshot(ws + 120, 40))
    assert cold.is_midwindow_gap(ws) is True

    # (2) boundary-not-arrived-yet: coverage before ws but no tick >= ws -> gap = False
    early = ChainlinkPriceStream()
    early._ingest_message(_snapshot(ws - 30, 30))  # ticks ws-30..ws-1, all < ws
    assert early.get_price_to_beat(ws) is None      # boundary not captured yet
    assert early.is_midwindow_gap(ws) is False       # but NOT a cold-start gap

    # (3) captured -> gap = False
    ok = ChainlinkPriceStream()
    ok._ingest_message(_snapshot(ws - 5, 20))
    assert ok.get_price_to_beat(ws) is not None
    assert ok.is_midwindow_gap(ws) is False


# ── Module singleton ───────────────────────────────────────────────────────────

def test_module_exposes_singleton_with_public_api():
    for attr in ("start", "stop", "get_current_price", "get_price_to_beat",
                 "is_fresh", "connected", "is_midwindow_gap"):
        assert hasattr(chainlink_stream, attr)
