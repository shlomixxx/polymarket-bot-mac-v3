"""
טסטים לשכבת ה-Market Order: place_market_order + dispatchers + slippage clamp.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ENGINE_DIR = Path(__file__).resolve().parents[1]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import live_clob  # noqa: E402


def test_clamp_slippage_price_buy():
    # BUY slippage = תקרה של מחיר גרוע (גבוה יותר מהיעד)
    assert live_clob._clamp_slippage_price(0.50, "BUY", 2.0) == pytest.approx(0.51, abs=1e-6)
    assert live_clob._clamp_slippage_price(0.50, "BUY", 0) == pytest.approx(0.50, abs=1e-6)
    # תקרה 0.999 — לא חוצים מעל
    assert live_clob._clamp_slippage_price(0.99, "BUY", 50.0) == pytest.approx(0.999, abs=1e-6)


def test_clamp_slippage_price_sell():
    # SELL slippage = מחיר נמוך יותר שנקבל
    assert live_clob._clamp_slippage_price(0.50, "SELL", 5.0) == pytest.approx(0.475, abs=1e-6)
    # רצפה 0.001 — לא יורדים מתחת
    assert live_clob._clamp_slippage_price(0.001, "SELL", 99.0) == pytest.approx(0.001, abs=1e-6)


def test_clamp_slippage_price_rejects_negative_slippage():
    # slippage שלילי נגרע ל-0
    assert live_clob._clamp_slippage_price(0.50, "BUY", -5.0) == pytest.approx(0.50, abs=1e-6)


def test_place_entry_order_limit_mode_delegates_to_limit(monkeypatch):
    """order_mode='limit' → קורא ל-place_limit_order עם אותם פרמטרים."""
    called = {}

    async def fake_limit(token_id, price, size, side):
        called.update({"token_id": token_id, "price": price, "size": size, "side": side})
        return {"ok": True, "order_id": "limit-1"}

    monkeypatch.setattr(live_clob, "place_limit_order", fake_limit)

    r = asyncio.run(
        live_clob.place_entry_order(
            "tok-1", 10.0, 0.50, "BUY",
            order_mode="limit", entry_slippage_pct=2.0,
        )
    )
    assert r["ok"]
    assert called == {"token_id": "tok-1", "price": 0.50, "size": 10.0, "side": "BUY"}


def test_place_entry_order_market_mode_converts_to_usd(monkeypatch):
    """order_mode='market' → BUY FOK עם amount=contracts*price דולרים."""
    called = {}

    async def fake_market(token_id, amount, side, *, order_type, slippage_cap_price):
        called.update({
            "token_id": token_id,
            "amount": amount,
            "side": side,
            "order_type": order_type,
            "slippage_cap_price": slippage_cap_price,
        })
        return {"ok": True, "order_id": "market-1"}

    monkeypatch.setattr(live_clob, "place_market_order", fake_market)

    r = asyncio.run(
        live_clob.place_entry_order(
            "tok-1", 10.0, 0.50, "BUY",
            order_mode="market", entry_slippage_pct=2.0,
        )
    )
    assert r["ok"]
    assert called["token_id"] == "tok-1"
    assert called["amount"] == pytest.approx(5.0)  # 10 * 0.50 = $5
    assert called["side"] == "BUY"
    assert called["order_type"] == "FOK"
    # 0.50 + 2% = 0.51
    assert called["slippage_cap_price"] == pytest.approx(0.51, abs=1e-6)
    # התגובה כוללת price+size לתאימות עם record_live_buy
    assert r["price"] == pytest.approx(0.50)
    assert r["size"] == pytest.approx(10.0)


def test_place_exit_order_market_mode_uses_fak_and_slippage(monkeypatch):
    """order_mode='market' → SELL FAK עם amount=חוזים ו-slippage cap מתחת ל-bid."""
    called = {}

    async def fake_market(token_id, amount, side, *, order_type, slippage_cap_price):
        called.update({
            "token_id": token_id,
            "amount": amount,
            "side": side,
            "order_type": order_type,
            "slippage_cap_price": slippage_cap_price,
        })
        # מתמלא מלא — אין ladder
        return {"ok": True, "order_id": "m-sell-1", "matched": amount}

    monkeypatch.setattr(live_clob, "place_market_order", fake_market)

    r = asyncio.run(
        live_clob.place_exit_order(
            "tok-1", 10.0, 0.60, order_mode="market", exit_slippage_pct=5.0,
        )
    )
    assert r["ok"]
    assert called["side"] == "SELL"
    assert called["amount"] == pytest.approx(10.0)
    assert called["order_type"] == "FAK"
    # 0.60 - 5% = 0.57
    assert called["slippage_cap_price"] == pytest.approx(0.57, abs=1e-6)
    # price+size נשמרו
    assert r["price"] == pytest.approx(0.60)


def test_place_exit_order_partial_fill_triggers_ladder(monkeypatch):
    """מילוי חלקי → retry ladder עם slippage מתרחב."""
    call_count = {"n": 0}

    async def fake_market(token_id, amount, side, *, order_type, slippage_cap_price):
        call_count["n"] += 1
        # נסיון ראשון: מתמלא רק 60%
        if call_count["n"] == 1:
            return {"ok": True, "matched": 6.0}
        # נסיון שני (ladder): מתמלא את השאר
        return {"ok": True, "matched": 4.0}

    monkeypatch.setattr(live_clob, "place_market_order", fake_market)
    # sleep מזויף כדי לא להאט את הטסט
    async def no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(live_clob.asyncio, "sleep", no_sleep)

    r = asyncio.run(
        live_clob.place_exit_order(
            "tok-1", 10.0, 0.60,
            order_mode="market", exit_slippage_pct=5.0, retry_max_attempts=3,
        )
    )
    assert r["ok"]
    assert call_count["n"] >= 2  # נכנס ל-ladder
    assert "ladder" in r


def test_retry_ladder_widens_slippage(monkeypatch):
    """בכל נסיון ה-slippage מתרחב (widen_factor)."""
    slips = []

    async def fake_market(token_id, amount, side, *, order_type, slippage_cap_price):
        slips.append(slippage_cap_price)
        # תמיד מילוי חלקי של 0
        return {"ok": True, "matched": 0.0}

    monkeypatch.setattr(live_clob, "place_market_order", fake_market)
    async def no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(live_clob.asyncio, "sleep", no_sleep)

    r = asyncio.run(
        live_clob._retry_market_sell_ladder(
            "tok", 10.0, bid=0.60, base_slippage_pct=2.0, max_attempts=3,
        )
    )
    # 3 נסיונות — slippage מתרחב (widen_factor=1.5)
    assert len(slips) == 3
    # כל slip נמוך יותר מהקודם (= slippage רחב יותר)
    assert slips[0] > slips[1] > slips[2]
    # לא סגר כלום
    assert r["sold_total_contracts"] == pytest.approx(0.0)


def test_retry_ladder_stops_on_min_size_error(monkeypatch):
    """שגיאת INVALID_ORDER_MIN_SIZE → עוצר את ה-ladder (אין טעם להמשיך)."""
    calls = {"n": 0}

    async def fake_market(token_id, amount, side, *, order_type, slippage_cap_price):
        calls["n"] += 1
        return {"ok": False, "error": "INVALID_ORDER_MIN_SIZE: amount below market minimum size"}

    monkeypatch.setattr(live_clob, "place_market_order", fake_market)
    async def no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(live_clob.asyncio, "sleep", no_sleep)

    r = asyncio.run(
        live_clob._retry_market_sell_ladder(
            "tok", 0.5, bid=0.60, base_slippage_pct=2.0, max_attempts=5,
        )
    )
    # עצר אחרי 1 נסיון — שאר ה-ladder לא רלוונטי
    assert calls["n"] == 1
    assert r["sold_total_contracts"] == pytest.approx(0.0)


def test_place_market_order_returns_error_code_on_balance_error(monkeypatch):
    """place_market_order שמפל ב-post_order עם 'not enough balance' → error_code='insufficient_onchain_balance'."""
    fake_client = MagicMock()
    fake_client.get_tick_size.return_value = 0.01
    fake_client.get_neg_risk.return_value = False
    fake_client.create_market_order.return_value = {"signed": True}

    def raise_balance(*a, **kw):
        raise Exception(
            "PolyApiException[status_code=400, error_message="
            "{'error': 'not enough balance / allowance: the balance is not enough -> balance: 2640, order amount: 1490000'}]"
        )
    fake_client.post_order.side_effect = raise_balance

    monkeypatch.setattr(live_clob, "build_trading_client", lambda: (fake_client, None))
    monkeypatch.setattr(live_clob, "check_balance_before_order", lambda usd: (True, None))
    monkeypatch.setattr(live_clob, "_fetch_conditional_balance_shares", lambda c, t: 1000.0)

    r = asyncio.run(
        live_clob.place_market_order(
            "tok-1", 1.495, "SELL", order_type="FAK", slippage_cap_price=0.57,
        )
    )
    assert r["ok"] is False
    assert r.get("error_code") == "insufficient_onchain_balance"
    assert "not enough balance" in r["error"].lower()


def test_place_exit_order_propagates_error_code(monkeypatch):
    """place_exit_order market-mode — error_code עובר דרך place_market_order אל ה-caller."""
    async def fake_market(token_id, amount, side, *, order_type, slippage_cap_price):
        return {"ok": False, "error": "not enough balance", "error_code": "insufficient_onchain_balance"}
    monkeypatch.setattr(live_clob, "place_market_order", fake_market)

    r = asyncio.run(
        live_clob.place_exit_order(
            "tok-1", 10.0, 0.60, order_mode="market", exit_slippage_pct=5.0,
        )
    )
    assert r["ok"] is False
    assert r.get("error_code") == "insufficient_onchain_balance"


def test_place_market_order_sell_uses_makingAmount_not_takingAmount(monkeypatch):
    """SELL: matched חייב להיות ב-CHOCES (makingAmount), לא ב-USDC (takingAmount).

    זה הבאג שגרם ל"חוזי רפאים": לפני התיקון matched=takingAmount (USDC)
    היה מחושב כשורת "נמכרו X חוזים" כש-X בפועל היה USDC מחולק במחיר. כתוצאה
    מכך p.contracts התכווץ באיטיות וניסיונות TP עוקבים נכשלו בבאלאנס.
    """
    fake_client = MagicMock()
    fake_client.get_tick_size.return_value = 0.01
    fake_client.get_neg_risk.return_value = False
    fake_client.create_market_order.return_value = {"signed": True}
    # תגובת CLOB נומינלית: מכרנו 4.94 חוזים וקיבלנו 3.16 USDC
    fake_client.post_order.return_value = {
        "orderID": "0xABC",
        "status": "matched",
        "makingAmount": "4.94",   # ← SHARES (חוזים שמסרנו)
        "takingAmount": "3.16",   # ← USDC (שקיבלנו)
    }
    monkeypatch.setattr(live_clob, "build_trading_client", lambda: (fake_client, None))
    monkeypatch.setattr(live_clob, "_fetch_conditional_balance_shares", lambda c, t: 10.0)

    r = asyncio.run(
        live_clob.place_market_order(
            "tok-1", 4.94, "SELL", order_type="FAK", slippage_cap_price=0.57,
        )
    )
    assert r["ok"] is True
    # matched חייב להיות 4.94 (חוזים), לא 3.16 (USDC).
    assert r["matched"] == pytest.approx(4.94, abs=1e-6)


def test_place_market_order_buy_uses_takingAmount(monkeypatch):
    """BUY: takingAmount הוא חוזים שקיבלנו — נשאר הבחירה הנכונה."""
    fake_client = MagicMock()
    fake_client.get_tick_size.return_value = 0.01
    fake_client.get_neg_risk.return_value = False
    fake_client.create_market_order.return_value = {"signed": True}
    fake_client.post_order.return_value = {
        "orderID": "0xABC",
        "status": "matched",
        "makingAmount": "5.00",  # USDC ששילמנו
        "takingAmount": "10.20",  # ← SHARES שקיבלנו
    }
    monkeypatch.setattr(live_clob, "build_trading_client", lambda: (fake_client, None))
    monkeypatch.setattr(live_clob, "check_balance_before_order", lambda usd: (True, None))

    r = asyncio.run(
        live_clob.place_market_order(
            "tok-1", 5.0, "BUY", order_type="FOK", slippage_cap_price=0.51,
        )
    )
    assert r["ok"] is True
    assert r["matched"] == pytest.approx(10.20, abs=1e-6)


def test_fetch_chain_shares_for_token_returns_float(monkeypatch):
    """fetch_chain_shares_for_token עוטף _fetch_conditional_balance_shares + build_trading_client."""
    fake_client = MagicMock()

    monkeypatch.setattr(live_clob, "build_trading_client", lambda: (fake_client, None))
    monkeypatch.setattr(
        live_clob, "_fetch_conditional_balance_shares",
        lambda c, t: 0.004778 if t == "tok-phantom" else None,
    )

    r_has = asyncio.run(live_clob.fetch_chain_shares_for_token("tok-phantom"))
    assert r_has == pytest.approx(0.004778, abs=1e-9)

    r_none = asyncio.run(live_clob.fetch_chain_shares_for_token("tok-unknown"))
    assert r_none is None


def test_fetch_chain_shares_for_token_returns_none_on_build_error(monkeypatch):
    """לא לכשל אם build_trading_client מחזיר שגיאה — רק None."""
    monkeypatch.setattr(live_clob, "build_trading_client", lambda: (None, "POLYMARKET_LIVE=0"))
    r = asyncio.run(live_clob.fetch_chain_shares_for_token("tok-1"))
    assert r is None


def test_fetch_conditional_balance_shares_small_raw_divides_by_1e6():
    """יתרה קטנה (raw=4083) = 0.004083 חוזים, לא 4083!

    זה הבאג שגרם ללוף של 'חוזי רפאים': כש-CLOB החזיר balance=4083 (מיקרו),
    ה-helper הישן (שהסתמך על _normalize_usdc_amount עם סף 1e4) החזיר 4083
    כאילו זה מספר חוזים מנורמל. התוצאה: הענף שסנכרן את p.contracts לפי השרשרת
    לא הופעל (כי chain_bal=4083 > p.contracts=0.27), והקוד נפל ל-reconcile
    שניפח בחזרה את הפוזיציה מ-Data API מעוכב.
    """
    fake_client = MagicMock()

    class FakeAssetType:
        CONDITIONAL = "CONDITIONAL"

    class FakeParams:
        def __init__(self, asset_type, token_id):
            self.asset_type = asset_type
            self.token_id = token_id

    fake_clob_types = MagicMock()
    fake_clob_types.AssetType = FakeAssetType
    fake_clob_types.BalanceAllowanceParams = FakeParams
    sys.modules["py_clob_client.clob_types"] = fake_clob_types

    fake_client.get_balance_allowance.return_value = {"balance": "4083"}
    r = live_clob._fetch_conditional_balance_shares(fake_client, "tok-1")
    assert r == pytest.approx(0.004083, abs=1e-9)

    # גם 260000 (0.26 חוזים) ו-6730000 (6.73 חוזים) מהלוג
    fake_client.get_balance_allowance.return_value = {"balance": "260000"}
    assert live_clob._fetch_conditional_balance_shares(fake_client, "tok-1") == pytest.approx(0.26, abs=1e-9)

    fake_client.get_balance_allowance.return_value = {"balance": "6730000"}
    assert live_clob._fetch_conditional_balance_shares(fake_client, "tok-1") == pytest.approx(6.73, abs=1e-9)

    # None/empty — לא לזרוק
    fake_client.get_balance_allowance.return_value = {"balance": None}
    assert live_clob._fetch_conditional_balance_shares(fake_client, "tok-1") is None

    fake_client.get_balance_allowance.return_value = {}
    assert live_clob._fetch_conditional_balance_shares(fake_client, "tok-1") is None
