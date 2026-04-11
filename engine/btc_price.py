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
# getRoundData(uint80) — סיבוב oracle ספציפי (להיסטוריה לפי זמן)
GET_ROUND_DATA_SELECTOR = "0x9a6fc8f5"
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


def _decode_v3_round_full(hex_data: str) -> Optional[tuple[int, float, int, int]]:
    """
    מפענח תשובת latestRoundData / getRoundData:
    (roundId, answer_usd, startedAt, updatedAt).
    """
    if not hex_data or hex_data in ("0x", "0x0"):
        return None
    h = hex_data[2:] if hex_data.startswith("0x") else hex_data
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return None
    if len(raw) < 128:
        return None
    # uint80 — בתוך מילת 32 בתים (ABI); מסכים עם מזהה מלא ל-getRoundData
    rid = int.from_bytes(raw[0:32], "big") & ((1 << 80) - 1)
    answer = int.from_bytes(raw[32:64], "big", signed=True)
    started_at = int.from_bytes(raw[64:96], "big")
    updated_at = int.from_bytes(raw[96:128], "big")
    return rid, float(answer) / 1e8, started_at, updated_at


def _encode_get_round_data(round_id: int) -> str:
    """ABI: getRoundData(uint80) — הארגומנט ב־32 בתים."""
    rid = int(round_id) & ((1 << 80) - 1)
    return GET_ROUND_DATA_SELECTOR + rid.to_bytes(32, "big").hex()


async def _polygon_eth_call(data: str) -> Optional[str]:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": CHAINLINK_BTC_USD_POLYGON, "data": data}, "latest"],
        "id": 1,
    }
    async with httpx.AsyncClient() as client:
        for rpc in POLYGON_PUBLIC_RPCS:
            try:
                r = await client.post(rpc, json=payload, timeout=8.0)
                r.raise_for_status()
                return r.json().get("result")
            except Exception:
                continue
    return None


async def fetch_chainlink_btc_usd_polygon_latest_full() -> Optional[tuple[int, float, int, int]]:
    """latestRoundData מלא — לחיפוש היסטורי."""
    res = await _polygon_eth_call(LATEST_ROUND_DATA_SELECTOR)
    if not res or not isinstance(res, str):
        return None
    return _decode_v3_round_full(res)


async def fetch_chainlink_btc_usd_polygon_get_round(round_id: int) -> Optional[tuple[int, float, int, int]]:
    data = _encode_get_round_data(round_id)
    res = await _polygon_eth_call(data)
    if not res or not isinstance(res, str):
        return None
    return _decode_v3_round_full(res)


async def fetch_chainlink_btc_usd_polygon_at_window_start(window_epoch_sec: int) -> Optional[float]:
    """
    מחיר הייחוס לפי פיד Chainlink על Polygon בתחילת החלון — לא latestRoundData.

    לוקחים את ערך הסיבוב האחרון ש־updatedAt <= זמן פתיחת החלון.
    (מזהי סיבוב גדולים מאוד — לכן חיפוש אחורה לינארי מהאחרון, לא בינארי על כל הטווח.)
    """
    now = int(time.time())
    if window_epoch_sec > now + 120:
        return await fetch_chainlink_btc_usd_polygon_latest()

    latest = await fetch_chainlink_btc_usd_polygon_latest_full()
    if latest is None:
        return None
    hi_id, _ans, _st, hi_upd = latest
    if hi_upd <= window_epoch_sec:
        return latest[1]

    rid = hi_id
    # עד כ־15 דק׳ בין עדכוני אורקל לעיתים — אלפי סיבובים אחורה מכסים גם חלון ארוך
    max_steps = 8000
    for _ in range(max_steps):
        if rid <= 0:
            break
        rd = await fetch_chainlink_btc_usd_polygon_get_round(rid)
        if rd is None:
            rid -= 1
            continue
        _r, ans, _st2, upd = rd
        if upd <= window_epoch_sec:
            return ans
        rid -= 1
    return None


async def fetch_chainlink_btc_usd_polygon_latest() -> Optional[float]:
    """מחיר BTC/USD אחרון מפיד Chainlink על Polygon (latestRound) — לא בהכרח Price to Beat."""
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
                r = await client.post(rpc, json=payload, timeout=8.0)
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
        # עיגול לסנטים — מפחית רעש float וקפיצות מזויפות בגרף הדשבורד
        try:
            p = round(float(price), 2)
        except (TypeError, ValueError):
            return
        self.points.append((t, p))
        if len(self.points) > self.max_points:
            self.points = self.points[-self.max_points :]

    def clear(self) -> None:
        self.points = []
