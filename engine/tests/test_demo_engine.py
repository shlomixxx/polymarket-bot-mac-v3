import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import data_source
from demo_engine import DemoEngine, DemoState, FEE_RATE, Position


@pytest.mark.asyncio
async def test_mark_to_market_no_positions_does_not_append_equity_history(tmp_path: Path):
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1234.0, positions=[], trades=[], equity_history=[])
    before = list(eng.state.equity_history)
    mark = await eng.mark_to_market()
    after = list(eng.state.equity_history)

    assert before == after
    assert mark["equity"] == pytest.approx(1234.0)
    assert mark["unrealized_usd"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_expire_all_outside_tokens_creates_expire_trade_and_removes_position(tmp_path: Path):
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    eng.state.positions = [
        Position(side="Up", contracts=10.0, avg_cost=0.4, token_id="old"),
        Position(side="Down", contracts=5.0, avg_cost=0.2, token_id="keep"),
    ]

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 99.0, "source": "binance_1m_proxy"}

    with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
        created = await eng.expire_all_outside_tokens(
            ("keep", "other"),
            context={
                "settled_epoch": 1_700_000_000,
                "settled_window_sec": 300,
                "execution": "live",
            },
        )

    assert len(created) == 1
    assert [p.token_id for p in eng.state.positions] == ["keep"]
    settle_loss = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(settle_loss) == 1
    t = settle_loss[0]
    assert created[0]["id"] == t["id"]
    assert t["token_id"] == "old"
    assert float(t["price"]) == 0.0
    # הפסד מלא של העלות (כולל עמלה בקירוב)
    expected_loss = -(0.4 * 10.0 * (1 + FEE_RATE))
    assert float(t["realized_pnl"]) == pytest.approx(expected_loss)
    assert t.get("execution") == "live"
    # leg_cost נחתם על עסקת הסגירה (מזין realized_pct ב-audit finalize)
    assert float(t["leg_cost"]) == pytest.approx(0.4 * 10.0 * (1 + FEE_RATE))
    # Task 5: כל עסקה שהתחשבנה מתויגת במקור-הנתונים הפעיל (ברירת מחדל polymarket).
    assert t.get("data_source") == "polymarket"


@pytest.mark.asyncio
async def test_expire_all_outside_tokens_tags_active_data_source_binance(tmp_path: Path):
    # Task 5: כשמקור-הנתונים הפעיל הוא binance, עסקת ההתחשבנות מתויגת בהתאם — לצורך
    # ייחוס סטטיסטיקות/היסטוריה לזירה (venue) הנכונה.
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    eng.state.positions = [Position(side="Up", contracts=10.0, avg_cost=0.4, token_id="old")]

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 99.0, "source": "binance_1m_proxy"}

    data_source.set_active("binance")
    try:
        with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
            await eng.expire_all_outside_tokens(
                ("keep",),
                context={"settled_epoch": 1_700_000_000, "settled_window_sec": 300},
            )
    finally:
        data_source.set_active("polymarket")

    settle_loss = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(settle_loss) == 1
    assert settle_loss[0].get("data_source") == "binance"


@pytest.mark.asyncio
async def test_attach_window_btc_to_tp_trade_tags_active_data_source(tmp_path: Path):
    # Task 5: מסלול ה-TP/Stop (SELL_TP/SELL_STOP) עובר דרך _attach_window_btc_to_tp_trade —
    # גם הוא חייב לתייג את מקור-הנתונים הפעיל בזמן ההתחשבנות.
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    trade: dict = {"epoch": 1_700_000_000, "window_sec": 300, "type": "SELL_TP"}

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 101.0, "source": "binance_1m_proxy"}

    data_source.set_active("binance")
    try:
        with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
            await eng._attach_window_btc_to_tp_trade(trade, side="Up")
    finally:
        data_source.set_active("polymarket")

    assert trade.get("data_source") == "binance"


@pytest.mark.asyncio
async def test_expire_keeps_position_whose_window_not_ended(tmp_path: Path):
    """הגנת flap: פוזיציה שהחלון שלה עדיין פעיל (לפי שעון) לא מותחשבנת מוקדם,
    גם אם הטוקן שלה אינו בחלון שהתגלה כרגע. פוזיציה שחלונה הסתיים — כן מותחשבנת.
    זה מונע את ריבוי ההפסדים באותו חלון."""
    eng = DemoEngine(state_path=tmp_path / "s.json")
    eng.state = DemoState(balance_usd=1000.0)
    now = time.time()
    active_ep = int(now // 300 * 300)  # תחילת החלון הנוכחי — נגמר ב-active_ep+300 (עתיד)
    eng.state.positions = [
        Position(side="Down", contracts=10.0, avg_cost=0.4, token_id="active_tok",
                 window_epoch=active_ep, window_sec=300),
        Position(side="Up", contracts=5.0, avg_cost=0.5, token_id="old_tok",
                 window_epoch=active_ep - 600, window_sec=300),  # חלון שהסתיים
    ]

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 99.0, "source": "test"}

    with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
        created = await eng.expire_all_outside_tokens(
            ("other_up", "other_down"),
            context={"settled_epoch": active_ep, "settled_window_sec": 300},
        )

    remaining = [p.token_id for p in eng.state.positions]
    assert "active_tok" in remaining, "פוזיציה בחלון פעיל לא אמורה להתחשבן מוקדם"
    assert "old_tok" not in remaining, "פוזיציה שחלונה הסתיים אמורה להתחשבן"
    assert any(t["token_id"] == "old_tok" for t in created)
    assert not any(t.get("token_id") == "active_tok" for t in created)


@pytest.mark.asyncio
async def test_expire_marks_reconcile_origin_when_no_session_id(tmp_path: Path):
    """פוזיציה שנכנסה דרך reconcile (חסרה ב-_session_by_token) חייבת להיות מסומנת
    reconcile_origin=True ב-SETTLE — כדי שה-UI לא יציג אותה כ«עסקה» ריקה של הריצה."""
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1000.0)
    eng.state.positions = [
        Position(side="Up", contracts=10.0, avg_cost=0.4, token_id="ghost-from-chain"),
        Position(side="Down", contracts=5.0, avg_cost=0.2, token_id="bot-opened"),
    ]
    # רק הטוקן שנפתח ע״י ה-BOT — יש לו session_id
    eng._session_by_token = {"bot-opened": "sess-abc"}

    async def _px(_ep: int, _ws: int):
        return {"start": 100.0, "end": 99.0, "source": "binance_1m_proxy"}

    with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
        created = await eng.expire_all_outside_tokens(
            ("other_up", "other_down"),
            context={
                "settled_epoch": 1_700_000_000,
                "settled_window_sec": 300,
                "execution": "live",
            },
        )

    assert len(created) == 2
    by_tid = {t["token_id"]: t for t in created}
    # הפוזיציה מה-chain (ללא session_id) — מסומנת reconcile_origin
    assert by_tid["ghost-from-chain"].get("reconcile_origin") is True
    assert by_tid["ghost-from-chain"].get("session_id") is None
    # הפוזיציה של ה-BOT — נשארת רגילה עם session_id
    assert by_tid["bot-opened"].get("reconcile_origin") is None
    assert by_tid["bot-opened"].get("session_id") == "sess-abc"


def test_export_csv_contains_headers_and_snapshot(tmp_path: Path):
    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=10_000.0)
    eng.state.trades = [
        {"ts": time.time(), "type": "EXPIRE_0", "side": "Up", "contracts": 1, "price": 0, "fee_est": 0, "token_id": "x", "realized_pnl": -1.0}
    ]
    eng.state.last_mark = {"ts": time.time(), "equity": 9999.0, "unrealized_usd": 0.0}
    csv_text = eng.export_csv()

    assert "ts,time,type,side,contracts,price,fee_est,token_id,session_id,realized_pnl" in csv_text
    assert "snapshot_ts,equity,unrealized_usd" in csv_text


def test_audit_buy_hook_creates_row_and_excludes_audit_inputs(tmp_path: Path, monkeypatch):
    """The BUY audit hook must (a) open an audit row keyed by session_id, and (b) NEVER let
    audit_inputs ride onto the persisted trade dict (invariant: keep demo_state.json lean)."""
    import importlib
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import audit_tracker
    importlib.reload(audit_tracker)  # bind audit.db to the temp DATA_ROOT

    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1_000.0)
    audit_inputs = {
        "mode": "live", "slug": "s", "epoch": 1, "window_sec": 300, "code_version": "t",
        "signal_result": None, "policy": {"loss_recovery_multiplier": 1.0},
        "book": {"ask_u": 0.5, "bid_u": 0.48, "ask_d": 0.5, "bid_d": 0.48},
        "provenance": {}, "regime": {"vol_bucket": "mid"},
    }
    eng.record_live_buy("Up", "tok-A", 10.0, 0.5,
                        context={"audit_inputs": audit_inputs, "gate": "test"})

    persisted = eng.state.trades[-1]
    assert "audit_inputs" not in persisted          # (b) not persisted onto the trade
    sid = persisted["session_id"]
    row = audit_tracker.get_audit(sid)
    assert row is not None                            # (a) audit row opened
    assert row["side"] == "Up"
    assert row["window_sec"] == 300
    assert row["settlement_status"] == "PENDING"


def test_audit_buy_hook_records_row_with_new_ledger_keys(tmp_path: Path, monkeypatch):
    """Regression: the audit-enrichment batch added new audit_inputs keys (market, raw_book_up,
    raw_book_down, raw_funding/funding_rate_pct, window_open_btc, spot_vs_open_pct). They are
    spread via **_inp into build_decision_snapshot, which previously had a FIXED keyword-only
    signature with no **extra -> TypeError -> open_row never ran -> NO audit row was written for
    ANY trade. This drives the REAL open_row path with those keys present and asserts (a) a row IS
    written, and (b) the new keys survive into the stored audit context."""
    import importlib
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import audit_tracker
    importlib.reload(audit_tracker)  # bind audit.db to the temp DATA_ROOT

    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1_000.0)
    audit_inputs = {
        "mode": "live", "slug": "s", "epoch": 1, "window_sec": 300, "code_version": "t",
        "signal_result": None, "policy": {"loss_recovery_multiplier": 1.0},
        "book": {"ask_u": 0.5, "bid_u": 0.48, "ask_d": 0.5, "bid_d": 0.48},
        "provenance": {}, "regime": {"vol_bucket": "mid"},
        # --- the NEW ledger keys added by the enrichment batch ---
        "market": {"edge_up": 0.03, "implied_up_prob": 0.5},
        "raw_book_up": {"bids": [[0.48, 100.0]], "asks": [[0.5, 80.0]]},
        "raw_book_down": {"bids": [[0.48, 90.0]], "asks": [[0.5, 70.0]]},
        "funding_rate_pct": 0.0123,
        "window_open_btc": 65000.0,
        "spot_vs_open_pct": 0.25,
    }
    eng.record_live_buy("Up", "tok-NEW", 10.0, 0.5,
                        context={"audit_inputs": audit_inputs, "gate": "test"})

    persisted = eng.state.trades[-1]
    assert "audit_inputs" not in persisted          # still not persisted onto the trade
    sid = persisted["session_id"]
    row = audit_tracker.get_audit(sid)
    assert row is not None                            # (a) a row WAS written despite the new keys
    assert row["side"] == "Up"
    assert row["settlement_status"] == "PENDING"
    # (b) the new keys survive into the stored audit context (top-level, readable names)
    ctx = row["context"]
    assert ctx["market"] == {"edge_up": 0.03, "implied_up_prob": 0.5}
    assert ctx["raw_book_up"] == {"bids": [[0.48, 100.0]], "asks": [[0.5, 80.0]]}
    assert ctx["raw_book_down"] == {"bids": [[0.48, 90.0]], "asks": [[0.5, 70.0]]}
    assert ctx["funding_rate_pct"] == pytest.approx(0.0123)
    assert ctx["window_open_btc"] == pytest.approx(65000.0)
    assert ctx["spot_vs_open_pct"] == pytest.approx(0.25)
    # existing snapshot keys must NOT be clobbered by the extra merge
    assert ctx["side"] == "Up"
    assert ctx["window_sec"] == 300
    assert ctx["schema_version"] == 1


def test_settlement_pnl_if_held_pure_arithmetic():
    """Recording-only counterfactual: payoff (=contracts if side==resolved, else 0) minus stake."""
    from demo_engine import _settlement_pnl_if_held
    # winning side held to resolution: 10 contracts -> 10*(1-FEE) payoff (fee netted like a real
    # win, so it's comparable to realized_pnl), minus the stake
    leg = 0.4 * 10.0 * (1 + FEE_RATE)
    won = _settlement_pnl_if_held(
        {"side": "Up", "contracts": 10.0, "leg_cost": leg, "resolved_outcome": "Up"})
    assert won == pytest.approx(round(10.0 * (1 - FEE_RATE) - leg, 4))
    # losing side held to resolution: $0 payoff, lose the whole stake
    lost = _settlement_pnl_if_held(
        {"side": "Down", "contracts": 10.0, "leg_cost": leg, "resolved_outcome": "Up"})
    assert lost == pytest.approx(-leg)
    # early exit (no resolved_outcome) -> None
    assert _settlement_pnl_if_held(
        {"side": "Up", "contracts": 10.0, "leg_cost": leg}) is None
    # unknown / missing leg_cost -> None (never raises)
    assert _settlement_pnl_if_held(
        {"side": "Up", "contracts": 10.0, "resolved_outcome": "Up"}) is None
    assert _settlement_pnl_if_held(
        {"side": "Up", "contracts": 10.0, "leg_cost": leg, "resolved_outcome": "UNKNOWN"}) is None


@pytest.mark.asyncio
async def test_settle_finalize_records_pnl_if_held(tmp_path: Path, monkeypatch):
    """A settled (held-to-resolution) trade must stamp settlement_pnl_if_held into the audit
    row's cf_exit_variants.pnl_if_held_to_resolution (read by audit_derive)."""
    import importlib
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    import audit_tracker
    importlib.reload(audit_tracker)  # bind audit.db to the temp DATA_ROOT

    eng = DemoEngine(state_path=tmp_path / "state.json")
    eng.state = DemoState(balance_usd=1_000.0)
    # Open an audit row via a BUY so finalize_row has a row to complete.
    audit_inputs = {
        "mode": "demo", "slug": "s", "epoch": 1_700_000_000, "window_sec": 300,
        "code_version": "t", "signal_result": None, "policy": {"loss_recovery_multiplier": 1.0},
        "book": {"ask_u": 0.4, "bid_u": 0.38, "ask_d": 0.6, "bid_d": 0.58},
        "provenance": {}, "regime": {},
    }
    eng.simulate_buy = eng.record_live_buy  # avoid book fetch; record path opens the same row
    eng.record_live_buy("Down", "old", 10.0, 0.4,
                        context={"audit_inputs": audit_inputs, "gate": "test"})
    sid = eng.state.trades[-1]["session_id"]
    # Position must mirror the BUY so the settle leg_cost matches.
    eng.state.positions = [Position(side="Down", contracts=10.0, avg_cost=0.4, token_id="old")]

    async def _px(_ep: int, _ws: int):
        # end < start -> resolves "Down"; our Down position wins
        return {"start": 100.0, "end": 99.0, "source": "test"}

    with patch("btc_price.fetch_window_start_end_btc_usd", AsyncMock(side_effect=_px)):
        await eng.expire_all_outside_tokens(
            ("other_up", "other_down"),
            context={"settled_epoch": 1_700_000_000, "settled_window_sec": 300})

    row = audit_tracker.get_audit(sid)
    assert row is not None
    leg = 0.4 * 10.0 * (1 + FEE_RATE)
    # winning Down side held to resolution -> 10*(1-FEE) payoff (fee netted) minus stake
    assert row["cf_exit_variants"]["pnl_if_held_to_resolution"] == pytest.approx(round(10.0 * (1 - FEE_RATE) - leg, 4))

