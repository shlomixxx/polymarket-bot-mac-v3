"""Bugfix (issue #2): במצב order_mode=market, גודל הפוזיציה חייב להיות מחושב לפי מחיר ה-fill
הצפוי (ה-ask), לא לפי entry_price_cents — אחרת ההוצאה בפועל גדולה פי ~2.5 מ-investment_usd."""
import pytest

from strategy_runner import (
    contracts_from_investment,
    effective_price_for_contract_qty,
    sizing_price_per_contract,
)


def test_market_sizing_uses_expected_fill_not_cap():
    """ask=0.51, cap=0.20, market 2% slippage -> מחיר ה-sizing הוא ~0.5202 (ה-fill), לא 0.20."""
    px = sizing_price_per_contract(0.51, 0.20, order_mode="market", entry_slippage_pct=2.0)
    assert px == pytest.approx(0.51 * 1.02)


def test_market_sizing_spend_matches_investment():
    """ההוצאה בפועל קרובה ל-investment_usd (לא פי ~2.5 כמו בבאג)."""
    inv, ask = 5.0, 0.51
    px = sizing_price_per_contract(ask, 0.20, order_mode="market", entry_slippage_pct=2.0)
    n = contracts_from_investment(inv, px, 1)
    spend = n * ask
    # באג: n=25, spend≈$12.75. תיקון: n≈9-10, spend≈$5 (עיגול חוזים שלמים -> אף פעם לא overshoot גדול).
    assert n <= 11
    assert spend <= inv * 1.15


def test_limit_sizing_unchanged():
    """מצב limit שומר על ההתנהגות הקיימת: min(cap, ask) (כמו effective_price_for_contract_qty)."""
    assert sizing_price_per_contract(0.51, 0.20, order_mode="limit") == pytest.approx(0.20)
    assert sizing_price_per_contract(0.11, 0.50, order_mode="limit") == pytest.approx(0.11)
    assert sizing_price_per_contract(0.51, 0.20, order_mode="limit") == effective_price_for_contract_qty(0.20, 0.51)


def test_market_sizing_falls_back_to_cap_when_no_ask():
    """בלי ask (אין ספר) — fallback ל-cap, כמו ההתנהגות הקיימת (קצה נדיר; כניסה לא תתבצע בלי ask)."""
    assert sizing_price_per_contract(None, 0.20, order_mode="market", entry_slippage_pct=2.0) == pytest.approx(0.20)
