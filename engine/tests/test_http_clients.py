"""טסטים ללקוחות HTTP משותפים עם keep-alive (תשתית 0.2)."""
import httpx

import btc_price


def test_get_binance_client_is_singleton():
    btc_price._BINANCE_CLIENT = None  # reset
    c1 = btc_price._get_binance_client()
    c2 = btc_price._get_binance_client()
    assert c1 is c2
    assert isinstance(c1, httpx.AsyncClient)


def test_get_binance_client_has_configured_timeouts():
    btc_price._BINANCE_CLIENT = None
    c = btc_price._get_binance_client()
    # timeout מוגדר במכוון (לא ברירת-מחדל) — מאשר שהלקוח נבנה עם ההגדרות שלנו ל-klines/spot
    assert c.timeout.connect == 2.0
    assert c.timeout.read == 10.0
