"""חלון 5m / 15m — גילוי שוק ו־window_sec."""
from __future__ import annotations

import market_discovery as md


def _minimal_event(slug: str) -> dict:
    return {
        "slug": slug,
        "title": "BTC test",
        "markets": [
            {
                "closed": False,
                "conditionId": "0x1",
                "endDate": "",
                "clobTokenIds": '["t1","t2"]',
                "outcomePrices": "[0.5,0.5]",
                "orderMinSize": 5,
            }
        ],
    }


def test_parse_5m_slug_has_window_300():
    am = md._parse_event(_minimal_event("btc-updown-5m-1700000000"))
    assert am is not None
    assert am.window_sec == 300
    assert am.epoch == 1700000000


def test_parse_15m_slug_has_window_900():
    am = md._parse_event(_minimal_event("btc-updown-15m-1700000000"))
    assert am is not None
    assert am.window_sec == 900
    assert am.epoch == 1700000000


def test_window_step_sec():
    assert md.window_step_sec("5m") == 300
    assert md.window_step_sec("15m") == 900


def test_seconds_until_window_end():
    import time

    now = time.time()
    epoch = int(now) - 60  # 60 שניות מתחילת חלון 5m
    left = md.seconds_until_window_end(epoch, 300)
    assert 200 <= left <= 241  # ~240 שניות נותרו (סביבה)
