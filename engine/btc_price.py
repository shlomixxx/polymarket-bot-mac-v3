"""מחיר BTC חי + פרוקסי לפתיחת חלון (Binance 1m) + Chainlink (Polygon) לייחוס כמו Polymarket."""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"

# Chainlink BTC/USD — Polygon PoS (אותו סוג oracle ש-Polymarket מציג כ-Price to beat)
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
POLYGON_PUBLIC_RPCS = (
    "https://polygon-bor.publicnode.com",
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
)


def _decode_chainlink_latest_round_answer(hex_data: str) -> Optional[float]:
    """מחזיר מחיר BTC/USD מ-hex של latestRoundData (AggregatorV3)."""
    if not hex_data or hex_data in ("0x", "0x0"):
        return None
    h = hex_data[2:] if hex_data.startswith("0x") else hex_data
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return None
    if len(raw) < 64:
        return None
    answer = int.from_bytes(raw[32:64], "big", signed=True)
    return float(answer) / 1e8


async def fetch_chainlink_btc_usd_polygon_latest() -> Optional[float]:
    """
    מחיר BTC/USD מפיד Chainlink על Polygon — קרוב למה שמוצג ב-Polymarket כ-Price to beat
    (לעומת פתיחת נר Binance 1m שיכולה להסטות עשרות דולרים).
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {
                "to": CHAINLINK_BTC_USD_POLYGON,
                "data": LATEST_ROUND_DATA_SELECTOR,
            },
            "latest",
        ],
        "id": 1,
    }
    async with httpx.AsyncClient() as client:
        for rpc in POLYGON_PUBLIC_RPCS:
            try:
                r = await client.post(rpc, json=payload, timeout=12.0)
                r.raise_for_status()
                result = r.json().get("result")
                if not result or not isinstance(result, str):
                    continue
                px = _decode_chainlink_latest_round_answer(result)
                if px is not None and px > 1000.0:
                    return px
            except Exception:
                continue
    return None


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
