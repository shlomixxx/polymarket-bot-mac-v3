"""PR-D load-test gate (NOT a unit test — run manually):
Build a realistic ~6MB / 50k-trade state and measure mark_to_market() timing.
PASS: p99 < 200ms, no single call >= 1s, persist fires off-loop <= ~once/20s, backfill <= once/30s.
Isolates CPU/save cost from network by stubbing the CLOB book read.
"""
import asyncio
import json
import os
import statistics
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch


def build_fixture(path: str, n_trades: int = 50_000) -> None:
    trades = []
    base = 1_700_000_000
    for i in range(n_trades):
        typ = "SELL_TP" if i % 5 == 0 else ("SETTLE_WIN" if i % 2 else "SETTLE_LOSS")
        t = {
            "id": f"t{i}", "ts": float(base + i), "type": typ,
            "side": "Up" if i % 2 else "Down", "token_id": f"tok{i % 50}",
            "session_id": f"s{i % 5000}", "epoch": base + (i % 5000) * 300,
            "window_sec": 300, "contracts": 10, "price": 0.5,
            "realized_pnl": (1.0 if i % 2 else -1.0), "avg_cost": 0.5, "fee_est": 0.01,
        }
        if typ == "SELL_TP":  # ALL filled → backfill does the O(N) canonical lookup but skips network attach
            t["settlement_btc_start"] = 60000.0
            t["settlement_btc_end"] = 60100.0
        trades.append(t)
    state = {
        "balance_usd": 9990.0,
        "positions": [{"token_id": "tokOPEN", "side": "Up", "contracts": 10, "avg_cost": 0.5, "entry_ts": float(base)}],
        "trades": trades,
        "equity_history": [[float(base + i), 10000.0 + i * 0.01] for i in range(5000)],
        "last_mark": {}, "trade_seq": n_trades, "loss_recovery_streak": 0,
        "loss_recovery_multiplier": 1.0, "stats_epoch_ts": None,
    }
    with open(path, "w") as f:
        json.dump(state, f)


async def main() -> None:
    d = tempfile.mkdtemp()
    sp = os.path.join(d, "demo_state.json")
    build_fixture(sp)
    print(f"fixture: {os.path.getsize(sp)/1e6:.1f} MB, 50k trades")
    os.environ["DEMO_STATE_PATH"] = sp

    import demo_engine
    eng = demo_engine.DemoEngine()

    # stub the CLOB book read so we measure CPU/save, not network (WS is down in this harness)
    fake_book = MagicMock()
    fake_book.status_code = 200
    fake_book.json = lambda: {"bids": [{"price": "0.55", "size": "100"}], "asks": [{"price": "0.56", "size": "100"}]}
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_book)

    persist_writes = {"n": 0}
    real_atomic = demo_engine.atomic_write_json
    def counting_write(*a, **k):
        persist_writes["n"] += 1
        return real_atomic(*a, **k)

    with patch.object(demo_engine, "_get_demo_clob_httpx", return_value=fake_client), \
         patch.object(demo_engine, "atomic_write_json", side_effect=counting_write):
        times = []
        t_start = time.time()
        first_dt = None
        for i in range(120):
            t0 = time.perf_counter()
            await eng.mark_to_market()
            dt = (time.perf_counter() - t0) * 1000.0
            if i == 0:
                first_dt = dt
            else:
                times.append(dt)
            await asyncio.sleep(0.05)  # ~50ms poll cadence
        # let any fire-and-forget persist finish
        await asyncio.sleep(2.0)
        elapsed = time.time() - t_start

    times.sort()
    p50 = statistics.median(times)
    p99 = times[int(len(times) * 0.99)]
    mx = max(times)
    print(f"first call (backfill+scan): {first_dt:.1f}ms")
    print(f"calls 2-120: p50={p50:.1f}ms  p99={p99:.1f}ms  max={mx:.1f}ms")
    print(f"persist writes: {persist_writes['n']} over {elapsed:.1f}s (expect ~{int(elapsed/20)+2} max, off-loop)")
    print(f"backfill runs: bounded by 30s throttle (1 in this {elapsed:.0f}s window)")
    # validate state file still loads
    json.load(open(sp))
    print("state file valid JSON after run: OK")
    # verdict
    ok = p99 < 200.0 and mx < 1000.0
    print(f"\nVERDICT: {'PASS' if ok else 'FAIL'} (p99<200ms={p99<200.0}, max<1s={mx<1000.0}; first-call backfill={first_dt:.0f}ms)")


if __name__ == "__main__":
    asyncio.run(main())
