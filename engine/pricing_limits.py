"""
ספי מחיר לבוט — תואם לטווח המסחר הטיפוסי ב-Polymarket (מניעת חוזים ענקיים / מחירי באג).
"""
from __future__ import annotations

# חוזי תוצאה ב-Polymarket בדרך כלל בין 1¢ ל־99¢ (0.01–0.99$ לחוזה).
MIN_LEGIT_SHARE_PRICE_USD = 0.01
MAX_LEGIT_SHARE_PRICE_USD = 0.99
