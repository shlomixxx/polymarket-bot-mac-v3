"""בדיקות ל-real_fee_adjusted_pnl — הנגזרת ה-read-only של P&L בעמלות-אמת של Polymarket.

מחקר עמידה (2026-06-08): Polymarket גובה עמלת taker דינמית לקריפטו, feeRate≈0.07,
עמלה-לצד כשבר-מהנפח = feeRate*(1-price). הדמו רושם flat 0.2%/צד (FEE_RATE).
real_fee_adjusted_pnl מחסירה רק את ה*תוספת* מעבר למה שהדמו כבר גבה.
"""
import pytest

from demo_engine import (
    FEE_RATE,
    CRYPTO_TAKER_FEE_RATE,
    real_fee_adjusted_pnl,
    _real_fee_fraction_of_notional,
)


def _make_buy_trade(price: float, contracts: float):
    """רגל-כניסה: leg_cost = avg_cost*contracts*(1+FEE_RATE) — בדיוק כמו שהמנוע רושם."""
    return price * contracts * (1.0 + FEE_RATE)


def test_fee_rate_constant_cited():
    # מתועד מהמחקר; אסור שייגרר מ-FEE_RATE של הדמו (0.002).
    assert CRYPTO_TAKER_FEE_RATE == 0.07
    assert FEE_RATE == 0.002


def test_fraction_of_notional_is_fee_rate_times_one_minus_price():
    # ב-p=0.5 ≈ 3.5% מהנפח; ב-p=0.7 ≈ 2.1%; ב-p=1.0 = 0.
    assert _real_fee_fraction_of_notional(0.5) == pytest.approx(0.035)
    assert _real_fee_fraction_of_notional(0.7) == pytest.approx(0.021)
    assert _real_fee_fraction_of_notional(1.0) == pytest.approx(0.0)


def test_known_settle_win_trade_expected_real_fee_net():
    """עסקה ידועה: כניסה p=0.50 ×100 חוזים, ניצחון ⇒ יציאה ב-1.0.

    realized הדמו = 100*1.0*(1-FEE_RATE) - 0.50*100*(1+FEE_RATE) = 99.80 - 50.10 = 49.70
    עמלת-דמו (שתי רגליים) = 0.002*0.50*100 + 0.002*1.0*100 = 0.10 + 0.20 = 0.30
    עמלת-אמת = 0.07*(1-0.5)*0.5*100 + 0.07*(1-1.0)*1.0*100 = 1.75 + 0 = 1.75
    תוספת-עמלה = 1.75 - 0.30 = 1.45 ⇒ real_fee_net = 49.70 - 1.45 = 48.25
    """
    leg_cost = _make_buy_trade(0.50, 100)
    realized = 100 * 1.0 * (1 - FEE_RATE) - leg_cost
    trade = {
        "type": "SETTLE_WIN",
        "contracts": 100,
        "price": 1.0,
        "leg_cost": leg_cost,
        "realized_pnl": realized,
        "ts": 100.0,
    }
    out = real_fee_adjusted_pnl([trade])
    assert out["sandbox_net"] == pytest.approx(49.70, abs=1e-2)
    assert out["fee_drag"] == pytest.approx(1.45, abs=1e-4)
    assert out["real_fee_net"] == pytest.approx(48.25, abs=1e-2)


def test_known_sell_tp_trade_expected_real_fee_net():
    """יציאה מוקדמת: כניסה p=0.40 ×50, מכירה ב-bid=0.60.

    עמלת-אמת על שתי הרגליים גבוהה מהדמו ב-1.58$ (0.84+0.84 vs 0.04+0.06).
    """
    leg_cost = _make_buy_trade(0.40, 50)
    realized = 0.60 * 50 * (1 - FEE_RATE) - leg_cost
    trade = {
        "type": "SELL_TP",
        "contracts": 50,
        "price": 0.60,
        "leg_cost": leg_cost,
        "realized_pnl": realized,
        "ts": 200.0,
    }
    out = real_fee_adjusted_pnl([trade])
    assert out["sandbox_net"] == pytest.approx(9.90, abs=1e-2)
    assert out["fee_drag"] == pytest.approx(1.58, abs=1e-4)
    assert out["real_fee_net"] == pytest.approx(8.32, abs=1e-2)


def test_settle_loss_only_entry_leg_charged():
    """הפסד: יציאה ב-0.0 ⇒ אין עמלת-יציאה בשני המודלים; רק רגל-הכניסה תורמת drag."""
    leg_cost = _make_buy_trade(0.50, 100)
    trade = {
        "type": "SETTLE_LOSS",
        "contracts": 100,
        "price": 0.0,
        "leg_cost": leg_cost,
        "realized_pnl": -leg_cost,
        "ts": 300.0,
    }
    out = real_fee_adjusted_pnl([trade])
    # entry: real 0.07*0.5*0.5*100=1.75, demo 0.002*0.5*100=0.10 ⇒ drag 1.65
    assert out["fee_drag"] == pytest.approx(1.65, abs=1e-4)
    assert out["real_fee_net"] == pytest.approx(out["sandbox_net"] - 1.65, abs=1e-4)


def test_ignores_open_reconcile_and_voided_trades():
    leg_cost = _make_buy_trade(0.50, 100)
    win = {
        "type": "SETTLE_WIN",
        "contracts": 100,
        "price": 1.0,
        "leg_cost": leg_cost,
        "realized_pnl": 100 * 1.0 * (1 - FEE_RATE) - leg_cost,
        "ts": 100.0,
    }
    buy = {"type": "BUY", "contracts": 100, "price": 0.5, "ts": 50.0}
    reconcile = {"type": "RECONCILE", "realized_pnl": 12.0, "price": 0.0, "ts": 60.0}
    voided = {
        "type": "SETTLE_UNKNOWN",
        "contracts": 100,
        "price": 0.0,
        "leg_cost": leg_cost,
        "realized_pnl": None,
        "voided": True,
        "ts": 70.0,
    }
    chain = {
        "type": "SETTLE_WIN",
        "contracts": 100,
        "price": 1.0,
        "leg_cost": leg_cost,
        "realized_pnl": 49.7,
        "reconcile_origin": True,  # נטענה מ-chain ⇒ לא נספרת
        "ts": 80.0,
    }
    out = real_fee_adjusted_pnl([buy, reconcile, voided, chain, win])
    # רק ה-win היחיד נספר.
    assert out["sandbox_net"] == pytest.approx(49.70, abs=1e-2)
    assert out["fee_drag"] == pytest.approx(1.45, abs=1e-4)


def test_since_ts_filters_pre_reset_trades():
    leg_cost = _make_buy_trade(0.50, 100)
    old = {
        "type": "SETTLE_WIN",
        "contracts": 100,
        "price": 1.0,
        "leg_cost": leg_cost,
        "realized_pnl": 49.7,
        "ts": 100.0,
    }
    new = {
        "type": "SETTLE_LOSS",
        "contracts": 100,
        "price": 0.0,
        "leg_cost": leg_cost,
        "realized_pnl": -leg_cost,
        "ts": 300.0,
    }
    out = real_fee_adjusted_pnl([old, new], since_ts=250.0)
    # רק ה-LOSS החדשה (ts=300) נכללת.
    assert out["sandbox_net"] == pytest.approx(-50.10, abs=1e-2)
    assert out["fee_drag"] == pytest.approx(1.65, abs=1e-4)


def test_empty_trades_returns_zeros():
    out = real_fee_adjusted_pnl([])
    assert out == {"sandbox_net": 0.0, "real_fee_net": 0.0, "fee_drag": 0.0}
