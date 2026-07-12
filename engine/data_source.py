"""מקור-הנתונים הפעיל למחירי BTC: "polymarket" (Chainlink stream) או "binance".

מצב תהליכי יחיד ("source of truth") שנשמר מסונכרן עם הקונפיג הנשמר: main.py קורא
set_active() בטעינת הקונפיג ובכל עדכון קונפיג, וצרכני-מחיר (btc_price, main) קוראים
get_active(). מודול טהור — בלי תלות ב-runner כדי למנוע import מעגלי.
"""
from __future__ import annotations

VALID_DATA_SOURCES: tuple[str, str] = ("polymarket", "binance")
_DEFAULT = "polymarket"

_active: str = _DEFAULT


def normalize(value) -> str:
    """מחזיר את value אם הוא מקור חוקי, אחרת polymarket (ברירת מחדל בטוחה)."""
    return value if value in VALID_DATA_SOURCES else _DEFAULT


def get_active() -> str:
    return _active


def set_active(value: str) -> str:
    global _active
    _active = normalize(value)
    return _active
