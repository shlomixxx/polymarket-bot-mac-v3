"""
בדיקות מקיפות: עסקאות מפסידות — רישום, סטטיסטיקה ו-Trigger Engine

בודק את הנקודות הבאות:
1. EXPIRE_0 נוצר עם realized_pnl שלילי
2. SELL במחיר נמוך ממחיר קנייה → realized_pnl שלילי
3. win_rate חישוב נכון כשיש גם רווחים וגם הפסדים
4. TriggerEngine._sync_window_epoch קורא ל-expire_all_outside_tokens
5. _trigger_positions מתנקה אחרי expire
6. TP מחושב על הפוזיציה המשולבת, לא לפי סלייס בודד
7. טיפול בפוזיציה שמחירה עלה ל-0 (EXPIRE_0 מלא)
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from demo_engine import DemoEngine, DemoState, FEE_RATE, Position


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_ask(ask: float):
    """מחזיר AsyncMock שמדמה DemoEngine.best_ask עם מחיר קבוע."""
    return AsyncMock(return_value=ask)


def _mock_bid_response(bid: float):
    """מחזיר mock ל-httpx.AsyncClient.get עם bid קבוע."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"bids": [{"price": str(bid)}], "asks": []}
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    return mock_client


def _patch_btc_up_wins():
    """סוף > תחילה ⇒ תוצאת השוק Up (לפירוק סימולציה)."""
    return patch(
        "btc_price.fetch_window_start_end_btc_usd",
        AsyncMock(return_value={"start": 100.0, "end": 101.0, "source": "binance_1m_proxy"}),
    )


def _patch_btc_down_wins():
    """סוף < תחילה ⇒ תוצאת השוק Down."""
    return patch(
        "btc_price.fetch_window_start_end_btc_usd",
        AsyncMock(return_value={"start": 101.0, "end": 100.0, "source": "binance_1m_proxy"}),
    )


_SETTLE_CTX = {"settled_epoch": 1_700_000_000, "settled_window_sec": 300}


# ══════════════════════════════════════════════════════════════════════
#  1. EXPIRE_0 — realized_pnl שלילי + רישום בעסקאות
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_expire_creates_negative_realized_pnl():
    """כשחלון נסגר ופוזיציה לא הגיעה ל-TP → SETTLE_LOSS כשהכיוון נגדנו."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=1000.0)
    eng.state.positions = [
        Position(side="Down", contracts=100.0, avg_cost=0.30, token_id="old_down"),
    ]

    with _patch_btc_up_wins():
        await eng.expire_all_outside_tokens(("new_up", "new_down"), context=_SETTLE_CTX)

    expire_trades = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(expire_trades) == 1

    t = expire_trades[0]
    assert float(t["realized_pnl"]) < 0, "SETTLE_LOSS חייב להיות הפסד"
    expected = -(0.30 * 100.0 * (1 + FEE_RATE))
    assert float(t["realized_pnl"]) == pytest.approx(expected, rel=1e-4)
    assert float(t["price"]) == 0.0
    assert t["token_id"] == "old_down"


@pytest.mark.asyncio
async def test_expire_removes_position_from_state():
    """אחרי פירוק הפוזיציה לא אמורה להיות ב-state.positions."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=500.0)
    eng.state.positions = [
        Position(side="Up", contracts=50.0, avg_cost=0.45, token_id="tok_expire"),
        Position(side="Down", contracts=20.0, avg_cost=0.10, token_id="tok_keep"),
    ]

    with _patch_btc_down_wins():
        await eng.expire_all_outside_tokens(("tok_keep",), context=_SETTLE_CTX)

    remaining = [p.token_id for p in eng.state.positions]
    assert "tok_expire" not in remaining
    assert "tok_keep" in remaining


@pytest.mark.asyncio
async def test_expire_multiple_positions_all_recorded():
    """כמה פוזיציות ישנות (אותו צד מפסיד) → כולן SETTLE_LOSS."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=1000.0)
    eng.state.positions = [
        Position(side="Up", contracts=10.0, avg_cost=0.40, token_id="old1"),
        Position(side="Up", contracts=20.0, avg_cost=0.15, token_id="old2"),
        Position(side="Up", contracts=5.0, avg_cost=0.50, token_id="keep"),
    ]

    with _patch_btc_down_wins():
        await eng.expire_all_outside_tokens(("keep",), context=_SETTLE_CTX)

    expired = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(expired) == 2
    expired_ids = {t["token_id"] for t in expired}
    assert expired_ids == {"old1", "old2"}
    for t in expired:
        assert float(t["realized_pnl"]) < 0


# ══════════════════════════════════════════════════════════════════════
#  2. SELL במחיר שונה מהעלות
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sell_below_cost_produces_negative_pnl():
    """מכירה כשהמחיר ירד מתחת לעלות → realized_pnl שלילי."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=1000.0)
    token = "tok_loss"
    avg_cost = 0.40
    contracts = 100.0
    eng.state.positions = [
        Position(side="Up", contracts=contracts, avg_cost=avg_cost, token_id=token),
    ]

    sell_bid = 0.20  # נמוך מהעלות
    with patch("demo_engine.httpx.AsyncClient", return_value=_mock_bid_response(sell_bid)):
        sell_result = await eng.simulate_sell_all(token)

    assert sell_result.get("ok"), f"simulate_sell_all נכשל: {sell_result}"
    sell_trades = [t for t in eng.state.trades if str(t.get("type", "")).startswith("SELL")]
    assert len(sell_trades) == 1

    t = sell_trades[0]
    assert float(t["realized_pnl"]) < 0, "מכירה מתחת לעלות צריכה להיות הפסד"

    proceeds = sell_bid * contracts * (1 - FEE_RATE)
    leg_cost = avg_cost * contracts * (1 + FEE_RATE)
    expected = proceeds - leg_cost
    assert float(t["realized_pnl"]) == pytest.approx(expected, rel=1e-4)


@pytest.mark.asyncio
async def test_sell_above_cost_produces_positive_pnl():
    """מכירה כשהמחיר עלה מעל העלות → realized_pnl חיובי."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=1000.0)
    token = "tok_win"
    eng.state.positions = [
        Position(side="Down", contracts=50.0, avg_cost=0.20, token_id=token),
    ]

    with patch("demo_engine.httpx.AsyncClient", return_value=_mock_bid_response(0.50)):
        result = await eng.simulate_sell_all(token)

    assert result.get("ok")
    sell_trades = [t for t in eng.state.trades if str(t.get("type", "")).startswith("SELL")]
    assert float(sell_trades[0]["realized_pnl"]) > 0


# ══════════════════════════════════════════════════════════════════════
#  3. חישוב win_rate עם שילוב רווחים והפסדים
# ══════════════════════════════════════════════════════════════════════

def _make_exit_trade(pnl: float, trade_type: str = "SELL_TP") -> dict:
    return {
        "ts": time.time(),
        "type": trade_type,
        "side": "Down",
        "contracts": 10,
        "price": 0.5,
        "fee_est": 0.001,
        "token_id": "t1",
        "realized_pnl": pnl,
    }


def _calc_winrate(trades: list[dict]) -> float:
    """מחקה את חישוב winRate ב-App.tsx."""
    exit_trades = [
        t for t in trades
        if t.get("realized_pnl") is not None
        and (
            t.get("type") == "EXPIRE_0"
            or t.get("type") in ("SETTLE_WIN", "SETTLE_LOSS", "SETTLE_UNKNOWN")
            or str(t.get("type", "")).startswith("SELL")
        )
    ]
    if not exit_trades:
        return 0.0
    wins = [t for t in exit_trades if float(t["realized_pnl"]) > 0]
    return len(wins) / len(exit_trades) * 100


def test_winrate_all_wins():
    trades = [_make_exit_trade(5.0), _make_exit_trade(3.0), _make_exit_trade(10.0)]
    assert _calc_winrate(trades) == pytest.approx(100.0)


def test_winrate_all_losses():
    trades = [_make_exit_trade(-5.0), _make_exit_trade(-3.0)]
    assert _calc_winrate(trades) == pytest.approx(0.0)


def test_winrate_mixed_50_percent():
    trades = [
        _make_exit_trade(5.0),   # רווח
        _make_exit_trade(-5.0),  # הפסד
    ]
    assert _calc_winrate(trades) == pytest.approx(50.0)


def test_winrate_includes_expire_0_as_loss():
    """SETTLE_LOSS / EXPIRE_0 עם pnl שלילי חייב להיכלל בחישוב ולהוריד win_rate."""
    trades = [
        _make_exit_trade(10.0, "SELL_TP"),       # רווח
        _make_exit_trade(-4.0, "SETTLE_LOSS"),       # הפסד מלא
        _make_exit_trade(-6.0, "EXPIRE_0"),       # תאימות אחורה
    ]
    rate = _calc_winrate(trades)
    # 1 win, 2 losses = 33.3%
    assert rate == pytest.approx(100 / 3, rel=1e-3)


def test_winrate_no_buy_trades_counted():
    """עסקאות BUY לא נספרות ב-win_rate."""
    trades = [
        {"type": "BUY", "realized_pnl": None, "ts": time.time(), "side": "Up",
         "contracts": 10, "price": 0.3, "fee_est": 0, "token_id": "t"},
        _make_exit_trade(5.0, "SELL_TP"),
    ]
    assert _calc_winrate(trades) == pytest.approx(100.0)


def test_winrate_none_pnl_not_counted():
    """עסקאות עם realized_pnl=None לא נספרות."""
    trades = [
        {"type": "SELL_TP", "realized_pnl": None, "ts": time.time(),
         "side": "Up", "contracts": 10, "price": 0.5, "fee_est": 0, "token_id": "t"},
        _make_exit_trade(3.0, "SELL_TP"),
    ]
    assert _calc_winrate(trades) == pytest.approx(100.0)


# ══════════════════════════════════════════════════════════════════════
#  4. TriggerEngine._sync_window_epoch → expire_all_outside_tokens
# ══════════════════════════════════════════════════════════════════════

def _make_mock_market(epoch: int, token_up="new_up", token_down="new_down"):
    m = MagicMock()
    m.epoch = epoch
    m.token_up = token_up
    m.token_down = token_down
    m.slug = f"btc-updown-5m-{epoch}"
    m.window_sec = 300
    return m


@pytest.mark.asyncio
async def test_trigger_sync_window_epoch_expires_old_positions(tmp_path):
    """כשהחלון משתנה, ה-TriggerEngine חייב לקרוא ל-expire_all_outside_tokens."""
    import os
    os.environ["DATA_ROOT"] = str(tmp_path)

    from trigger_engine import TriggerConfig, TriggerEngine

    eng = TriggerEngine()
    eng.config = TriggerConfig(btc_window="5m", mode="dca_pulse", active=True)

    demo = DemoEngine()
    demo.state = DemoState(balance_usd=1000.0)
    demo.state.positions = [
        Position(side="Down", contracts=100.0, avg_cost=0.08, token_id="old_token"),
    ]
    eng._demo = demo
    eng.current_window_epoch = 1_000_000

    mock_market = _make_mock_market(1_000_300)

    with _patch_btc_up_wins(), patch(
        "market_discovery.discover_active_btc_window", new=AsyncMock(return_value=mock_market)
    ):
        await eng._sync_window_epoch()

    assert eng.current_window_epoch == 1_000_300

    expire_trades = [t for t in demo.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(expire_trades) == 1, "חייב להיות SETTLE_LOSS על הטוקן הישן (Down מול Up)"
    assert float(expire_trades[0]["realized_pnl"]) < 0
    assert expire_trades[0]["token_id"] == "old_token"
    assert len(demo.state.positions) == 0


@pytest.mark.asyncio
async def test_trigger_sync_does_not_expire_on_first_window(tmp_path):
    """כש-current_window_epoch == 0 (הפעלה ראשונה), אין expire."""
    import os
    os.environ["DATA_ROOT"] = str(tmp_path)

    from trigger_engine import TriggerConfig, TriggerEngine

    eng = TriggerEngine()
    demo = DemoEngine()
    demo.state = DemoState(balance_usd=1000.0)
    demo.state.positions = [
        Position(side="Up", contracts=10.0, avg_cost=0.40, token_id="existing"),
    ]
    eng._demo = demo
    eng.current_window_epoch = 0  # הפעלה ראשונה

    mock_market = _make_mock_market(999_000)

    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=mock_market)):
        await eng._sync_window_epoch()

    # ב-epoch=0 אין expire
    expire_trades = [t for t in demo.state.trades if t.get("type") == "EXPIRE_0"]
    assert len(expire_trades) == 0
    assert len(demo.state.positions) == 1  # פוזיציה נשארה


@pytest.mark.asyncio
async def test_trigger_sync_no_expire_when_same_epoch(tmp_path):
    """כשהחלון לא השתנה — אין expire ואין שינוי."""
    import os
    os.environ["DATA_ROOT"] = str(tmp_path)

    from trigger_engine import TriggerConfig, TriggerEngine

    eng = TriggerEngine()
    demo = DemoEngine()
    demo.state = DemoState(balance_usd=1000.0)
    demo.state.positions = [
        Position(side="Up", contracts=10.0, avg_cost=0.40, token_id="tok"),
    ]
    eng._demo = demo
    eng.current_window_epoch = 5_000_000

    mock_market = _make_mock_market(5_000_000)  # אותו epoch

    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=mock_market)):
        await eng._sync_window_epoch()

    expire_trades = [t for t in demo.state.trades if t.get("type") == "EXPIRE_0"]
    assert len(expire_trades) == 0


# ══════════════════════════════════════════════════════════════════════
#  5. _trigger_positions מתנקה אחרי expire
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_trigger_positions_cleared_after_expire(tmp_path):
    """טוקנים שפגה תוקפם → מוסרים מ-_trigger_positions."""
    import os
    os.environ["DATA_ROOT"] = str(tmp_path)

    from trigger_engine import TriggerConfig, TriggerEngine

    eng = TriggerEngine()
    demo = DemoEngine()
    demo.state = DemoState(balance_usd=1000.0)
    old_token = "old_tok"
    demo.state.positions = [
        Position(side="Down", contracts=50.0, avg_cost=0.06, token_id=old_token),
    ]
    eng._demo = demo
    eng.current_window_epoch = 2_000_000
    eng._trigger_positions[old_token] = {
        "side": "Down", "avg_cost": 0.06, "contracts": 50,
        "tp_pct": 20.0, "entry_ts": time.time(),
    }

    mock_market = _make_mock_market(2_000_300)

    with _patch_btc_up_wins(), patch(
        "market_discovery.discover_active_btc_window", new=AsyncMock(return_value=mock_market)
    ):
        await eng._sync_window_epoch()

    assert old_token not in eng._trigger_positions, "טוקן שפג תוקפו חייב להוסר מ-_trigger_positions"


@pytest.mark.asyncio
async def test_trigger_positions_retained_if_still_valid(tmp_path):
    """טוקן שעדיין בפוזיציה פעילה — נשאר ב-_trigger_positions."""
    import os
    os.environ["DATA_ROOT"] = str(tmp_path)

    from trigger_engine import TriggerConfig, TriggerEngine

    eng = TriggerEngine()
    demo = DemoEngine()
    demo.state = DemoState(balance_usd=1000.0)
    new_up = "new_up"
    demo.state.positions = [
        Position(side="Up", contracts=30.0, avg_cost=0.25, token_id=new_up),
    ]
    eng._demo = demo
    eng.current_window_epoch = 3_000_000
    eng._trigger_positions[new_up] = {
        "side": "Up", "avg_cost": 0.25, "contracts": 30,
        "tp_pct": 20.0, "entry_ts": time.time(),
    }

    mock_market = _make_mock_market(3_000_300, token_up=new_up, token_down="new_down")

    with patch("market_discovery.discover_active_btc_window", new=AsyncMock(return_value=mock_market)):
        await eng._sync_window_epoch()

    assert new_up in eng._trigger_positions, "טוקן שעדיין בדמו לא אמור להימחק"


# ══════════════════════════════════════════════════════════════════════
#  6. TP מחושב על פוזיציה משולבת (DCA מצטבר)
# ══════════════════════════════════════════════════════════════════════

def test_tp_target_reflects_combined_dca_position():
    """
    בפוזיציה DCA עם 3 סלייסים, TP target חייב להתבסס על avg_cost המשולב.
    """
    entries = [
        (100, 0.40),
        (200, 0.25),
        (500, 0.10),
    ]
    total_contracts = sum(c for c, _ in entries)
    total_cost = sum(c * p for c, p in entries)
    combined_avg = total_cost / total_contracts

    tp_pct = 20.0
    tp_target_price = combined_avg * (1 + tp_pct / 100)
    tp_target_profit = total_cost * (tp_pct / 100)

    # TP target חייב להיות גבוה ממחיר הסלייס האחרון
    last_entry_price = entries[-1][1]
    assert tp_target_price > last_entry_price, (
        f"TP @ {tp_target_price:.4f}$ חייב להיות > מחיר סלייס אחרון {last_entry_price}$"
    )

    expected_avg = (100 * 0.40 + 200 * 0.25 + 500 * 0.10) / 800
    assert combined_avg == pytest.approx(expected_avg, rel=1e-4)
    assert tp_target_profit == pytest.approx(total_cost * 0.20, rel=1e-4)


def test_tp_combined_higher_than_per_slice():
    """TP ממוצע משוקלל תמיד גבוה יותר מ-TP של הסלייס הזול ביותר."""
    slices = [(50, 0.48), (100, 0.30), (200, 0.15), (400, 0.07)]
    total_c = sum(c for c, _ in slices)
    total_cost = sum(c * p for c, p in slices)
    combined_avg = total_cost / total_c

    cheapest_price = min(p for _, p in slices)
    tp_combined = combined_avg * 1.20
    tp_cheapest = cheapest_price * 1.20

    assert tp_combined > tp_cheapest, "TP על הפוזיציה המשולבת חייב להיות גבוה מ-TP של הסלייס הזול"


# ══════════════════════════════════════════════════════════════════════
#  7. קנייה + expire → balance לא חוזר
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_balance_decreases_on_buy_and_stays_after_expire():
    """קנייה מורידה יתרה; SETTLE_LOSS לא מזכה (הפסד מלא)."""
    initial_balance = 1000.0
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=initial_balance)

    token = "exp_tok"
    ask_price = 0.30
    contracts = 20.0

    with patch.object(eng, "best_ask", return_value=ask_price):
        buy_result = await eng.simulate_market_buy(
            "Down", token, contracts, limit_price=ask_price, context={}
        )
    assert buy_result.get("ok"), f"קנייה נכשלה: {buy_result}"

    balance_after_buy = eng.state.balance_usd
    assert balance_after_buy < initial_balance

    with _patch_btc_up_wins():
        await eng.expire_all_outside_tokens(("other_token",), context=_SETTLE_CTX)

    balance_after_expire = eng.state.balance_usd
    # הפסד בפירוק — אין זיכוי
    assert balance_after_expire == pytest.approx(balance_after_buy, rel=1e-6)

    expire_trades = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(expire_trades) == 1
    assert float(expire_trades[0]["realized_pnl"]) < 0


# ══════════════════════════════════════════════════════════════════════
#  8. סשן שלם עם win + loss → win_rate = 50%
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_session_with_win_and_loss_produces_50pct_winrate():
    """
    סשן שלם:
      - עסקה 1: קנייה + מכירה ברווח → win
      - עסקה 2: קנייה + מעבר חלון (EXPIRE_0) → loss
    win_rate חייב להיות 50%.
    """
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=2000.0)

    # עסקה 1 — קנייה ומכירה ברווח
    tok_win = "win_tok"
    with patch.object(eng, "best_ask", return_value=0.30):
        await eng.simulate_market_buy("Up", tok_win, 20.0, limit_price=0.30, context={})
    with patch("demo_engine.httpx.AsyncClient", return_value=_mock_bid_response(0.50)):
        await eng.simulate_sell_all(tok_win)  # מכירה ברווח

    # עסקה 2 — קנייה + expire (לא הגיע ל-TP)
    tok_loss = "loss_tok"
    with patch.object(eng, "best_ask", return_value=0.45):
        await eng.simulate_market_buy("Down", tok_loss, 10.0, limit_price=0.45, context={})
    with _patch_btc_up_wins():
        await eng.expire_all_outside_tokens((tok_win,), context=_SETTLE_CTX)  # loss_tok לא ברשימה

    win_rate = _calc_winrate(eng.state.trades)
    assert win_rate == pytest.approx(50.0, rel=1e-2), (
        f"win_rate צריך להיות 50%, קיבלנו {win_rate:.1f}%"
    )


@pytest.mark.asyncio
async def test_three_losses_produces_zero_winrate():
    """שלושה הפסדים (SETTLE_LOSS) → win_rate = 0%."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=2000.0)

    tokens = ["tok_a", "tok_b", "tok_c"]
    for tok in tokens:
        with patch.object(eng, "best_ask", return_value=0.40):
            await eng.simulate_market_buy("Up", tok, 10.0, limit_price=0.40, context={})

    # expire לכולם
    with _patch_btc_down_wins():
        await eng.expire_all_outside_tokens(("tok_irrelevant",), context=_SETTLE_CTX)

    win_rate = _calc_winrate(eng.state.trades)
    assert win_rate == pytest.approx(0.0)

    expire_trades = [t for t in eng.state.trades if t.get("type") == "SETTLE_LOSS"]
    assert len(expire_trades) == 3


@pytest.mark.asyncio
async def test_winrate_100_only_when_all_sold_profitably():
    """100% win_rate רק כשכל היציאות הן ברווח."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=2000.0)

    for i in range(3):
        tok = f"tok_{i}"
        with patch.object(eng, "best_ask", return_value=0.25):
            await eng.simulate_market_buy("Up", tok, 10.0, limit_price=0.25, context={})
        with patch("demo_engine.httpx.AsyncClient", return_value=_mock_bid_response(0.60)):
            await eng.simulate_sell_all(tok)

    rate = _calc_winrate(eng.state.trades)
    assert rate == pytest.approx(100.0)

    exits = [t for t in eng.state.trades if str(t.get("type", "")).startswith("SELL")]
    assert len(exits) == 3


# ══════════════════════════════════════════════════════════════════════
#  9. EXPIRE_0 עם realized_pnl מדויק — חישוב fee
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_expire_pnl_includes_fee():
    """הפסד ב-SETTLE_LOSS כולל עמלת הכניסה (avg_cost * contracts * (1 + FEE_RATE))."""
    eng = DemoEngine()
    eng.state = DemoState(balance_usd=1000.0)

    avg_cost = 0.12
    contracts = 200.0
    token = "fee_test_tok"
    eng.state.positions = [
        Position(side="Down", contracts=contracts, avg_cost=avg_cost, token_id=token),
    ]

    with _patch_btc_up_wins():
        await eng.expire_all_outside_tokens(("other",), context=_SETTLE_CTX)

    t = next(x for x in eng.state.trades if x.get("type") == "SETTLE_LOSS")
    expected_loss = -(avg_cost * contracts * (1 + FEE_RATE))
    assert float(t["realized_pnl"]) == pytest.approx(expected_loss, rel=1e-5)
    # הפסד חייב לכלול את ה-FEE_RATE
    naive_loss = -(avg_cost * contracts)
    assert abs(float(t["realized_pnl"])) > abs(naive_loss), "הפסד חייב לכלול עמלה"
