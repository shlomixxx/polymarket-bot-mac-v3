"""מחיר BTC חי + פרוקסי לפתיחת חלון (Binance 1m open — קרוב ל-Chainlink לתצוגה)."""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


async def fetch_btc_spot_usdt() -> float:
    async with httpx.AsyncClient() as client:
        r = await client.get(BINANCE_TICKER, params={"symbol": "BTCUSDT"}, timeout=10.0)
        r.raise_for_status()
        return float(r.json()["price"])


async def fetch_open_price_at_window_start(window_epoch_sec: int) -> Optional[float]:
    """מחיר פתיחת נר 1m שמתחיל ב-window_epoch (Unix שניות)."""
    start_ms = window_epoch_sec * 1000
    async with httpx.AsyncClient() as client:
        r = await client.get(
            BINANCE_KLINES,
            params={"symbol": "BTCUSDT", "interval": "1m", "startTime": start_ms, "limit": 1},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return float(data[0][1])


async def fetch_close_price_at_window_end(window_epoch_sec: int, window_sec: int) -> Optional[float]:
    """סגירת נר ה-1m האחרון בטווח [epoch, epoch+window_sec) — מתאים לסוף חלון Up/Down (פרוקסי Binance)."""
    if window_sec < 60:
        return None
    # הנר האחרון מתחיל ב-epoch + window_sec - 60 ונסגר ב-epoch + window_sec
    last_candle_open_ms = (window_epoch_sec + window_sec - 60) * 1000
    async with httpx.AsyncClient() as client:
        r = await client.get(
            BINANCE_KLINES,
            params={
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": last_candle_open_ms,
                "limit": 1,
            },
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return float(data[0][4])


async def fetch_window_start_end_btc_usd(
    window_epoch_sec: int, window_sec: int
) -> dict[str, Any]:
    """
    זוג מחירי התחלה/סוף לפירוק סימולציה (אותה מתודולוגיה כמו price_to_beat).
    הרזולוציה הרשמית ב-Polymarket היא Chainlink — כאן binance_1m_proxy לשקיפות.
    """
    start = await fetch_open_price_at_window_start(window_epoch_sec)
    end = await fetch_close_price_at_window_end(window_epoch_sec, window_sec)
    return {
        "start": start,
        "end": end,
        "source": "binance_1m_proxy",
    }


class PriceHistoryBuffer:
    """דגימות למסך גרף בתוך החלון."""

    def __init__(self, max_points: int = 120):
        self.max_points = max_points
        self.points: list[tuple[float, float]] = []

    def add(self, price: float) -> None:
        t = time.time()
        self.points.append((t, price))
        if len(self.points) > self.max_points:
            self.points = self.points[-self.max_points :]

    def clear(self) -> None:
        self.points = []
