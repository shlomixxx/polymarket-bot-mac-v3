"""מחיר BTC חי + פרוקסי לפתיחת חלון (Binance 1m) + Chainlink (Polygon) לייחוס כמו Polymarket."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"

# לקוח httpx משותף ל-Binance עם keep-alive (תשתית 0.2 / B-6): חוסך TLS handshake חדש בכל
# קריאת spot/klines. timeout ברירת-מחדל מתאים ל-klines (10s); קריאת ה-spot עוקפת ל-4s לכל קריאה.
_BINANCE_CLIENT: Optional[httpx.AsyncClient] = None


def _get_binance_client() -> httpx.AsyncClient:
    global _BINANCE_CLIENT
    if _BINANCE_CLIENT is None:
        _BINANCE_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _BINANCE_CLIENT


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


_POLYGON_CLIENT: Optional[httpx.AsyncClient] = None
_POLYGON_RPC_PREFER_IDX: int = 0


def _get_polygon_client() -> httpx.AsyncClient:
    global _POLYGON_CLIENT
    if _POLYGON_CLIENT is None:
        _POLYGON_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=3.0, write=3.0, pool=3.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _POLYGON_CLIENT


async def _polygon_eth_call(data: str) -> Optional[str]:
    """eth_call חוזר עם timeout קצר ו-RPC נדבק (sticky) לזה שעבד אחרון — לא נחסם על RPC איטי."""
    global _POLYGON_RPC_PREFER_IDX
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": CHAINLINK_BTC_USD_POLYGON, "data": data}, "latest"],
        "id": 1,
    }
    client = _get_polygon_client()
    n = len(POLYGON_PUBLIC_RPCS)
    for i in range(n):
        idx = (_POLYGON_RPC_PREFER_IDX + i) % n
        rpc = POLYGON_PUBLIC_RPCS[idx]
        try:
            r = await client.post(rpc, json=payload, timeout=3.0)
            r.raise_for_status()
            res = r.json().get("result")
            if res:
                _POLYGON_RPC_PREFER_IDX = idx
                return res
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


_CHAINLINK_AT_WINDOW_CACHE: dict[int, float] = {}

# A-7: cache קבוע למחירי open/close של settlement. נרות 1m סגורים הם immutable, אז ערך
# שנמשך פעם אחת תקף לתמיד. מאחסנים *רק* ערכים סופיים (לא None) — close נשמר רק אחרי
# שהנר נסגר (בדיקת closeTime>now). מפתח: open לפי epoch, close לפי (epoch, window_sec).
# זו אינה תקרית ה-martingale: היא נבעה מ-None שהפך ל-loss; כאן None לעולם לא נשמר.
_OPEN_PRICE_CACHE: dict[int, float] = {}
_CLOSE_PRICE_CACHE: dict[tuple[int, int], float] = {}
_SETTLEMENT_CACHE_MAX = 1024


def _prune_settlement_cache(cache: dict) -> None:
    """שומר את החלונות העדכניים ביותר; מסיר ישנים (מפתח קטן = epoch מוקדם) כדי להגביל זיכרון."""
    excess = len(cache) - _SETTLEMENT_CACHE_MAX
    if excess > 0:
        for k in sorted(cache.keys())[:excess]:
            cache.pop(k, None)


async def fetch_chainlink_btc_usd_polygon_at_window_start(window_epoch_sec: int) -> Optional[float]:
    """
    מחיר הייחוס לפי פיד Chainlink על Polygon בתחילת החלון — לא latestRoundData.

    לוקחים את ערך הסיבוב האחרון ש־updatedAt <= זמן פתיחת החלון.
    (מזהי סיבוב גדולים מאוד — לכן חיפוש אחורה לינארי מהאחרון, לא בינארי על כל הטווח.)
    """
    cached = _CHAINLINK_AT_WINDOW_CACHE.get(window_epoch_sec)
    if cached is not None:
        return cached

    now = int(time.time())
    if window_epoch_sec > now + 120:
        return await fetch_chainlink_btc_usd_polygon_latest()

    latest = await fetch_chainlink_btc_usd_polygon_latest_full()
    if latest is None:
        return None
    hi_id, _ans, _st, hi_upd = latest
    if hi_upd <= window_epoch_sec:
        _CHAINLINK_AT_WINDOW_CACHE[window_epoch_sec] = latest[1]
        return latest[1]

    rid = hi_id
    # אורקל מתעדכן בערך כל heartbeat (כמה דקות) — 200 סיבובים כיסוי בטוח לחלון 5/15 דק׳.
    # קודם עברנו על 8000 — חסם את ה-event loop לעשרות שניות כשרשת RPC איטית.
    max_steps = 200
    for _ in range(max_steps):
        if rid <= 0:
            break
        rd = await fetch_chainlink_btc_usd_polygon_get_round(rid)
        if rd is None:
            rid -= 1
            continue
        _r, ans, _st2, upd = rd
        if upd <= window_epoch_sec:
            _CHAINLINK_AT_WINDOW_CACHE[window_epoch_sec] = ans
            return ans
        rid -= 1
    return None


async def fetch_chainlink_btc_usd_polygon_latest() -> Optional[float]:
    """מחיר BTC/USD אחרון מפיד Chainlink על Polygon (latestRound) — לא בהכרח Price to Beat.
    C-6: עובר דרך _polygon_eth_call המאוגד (keep-alive + RPC sticky failover) במקום לקוח ad-hoc."""
    result = await _polygon_eth_call(LATEST_ROUND_DATA_SELECTOR)
    if not result or not isinstance(result, str):
        return None
    px = _decode_chainlink_latest_round_answer(result)
    if px is not None and px > 1000.0:
        return px
    return None


_BTC_SPOT_CACHE: dict[str, Any] = {"price": None, "ts": 0.0}
_BTC_SPOT_CACHE_TTL_SEC = 1.0
_BTC_SPOT_STALE_TTL_SEC = 30.0


async def fetch_btc_spot_usdt() -> float:
    """מחיר BTC ספוט מ-Binance עם קאש 1s ו-fallback למחיר אחרון תקף עד 30s במקרה כשל."""
    now = time.time()
    cached_price = _BTC_SPOT_CACHE.get("price")
    cached_ts = float(_BTC_SPOT_CACHE.get("ts") or 0.0)
    if cached_price is not None and (now - cached_ts) <= _BTC_SPOT_CACHE_TTL_SEC:
        return float(cached_price)
    try:
        client = _get_binance_client()
        r = await client.get(BINANCE_TICKER, params={"symbol": "BTCUSDT"}, timeout=4.0)
        r.raise_for_status()
        price = float(r.json()["price"])
        _BTC_SPOT_CACHE["price"] = price
        _BTC_SPOT_CACHE["ts"] = time.time()
        return price
    except Exception:
        if cached_price is not None and (now - cached_ts) <= _BTC_SPOT_STALE_TTL_SEC:
            return float(cached_price)
        raise


async def fetch_btc_current_usd() -> tuple[float, str]:
    """מחיר BTC נוכחי — מעדיף את פיד Chainlink Data Stream של Polymarket (המקור שלפיו השוק נסגר);
    נופל ל-Binance spot רק אם הפיד לא טרי/לא זמין.

    מחזיר (price, source) כאשר source ∈ {"chainlink_stream", "binance_fallback"}.
    ה-import של הפיד עצל כדי לא לקשור את btc_price ל-chainlink_price_stream בזמן טעינה.
    """
    try:
        from chainlink_price_stream import chainlink_stream

        cur = chainlink_stream.get_current_price()
        if cur is not None:
            return float(cur["value"]), "chainlink_stream"
    except Exception:
        pass
    price = await fetch_btc_spot_usdt()
    return price, "binance_fallback"


async def fetch_open_price_at_window_start(window_epoch_sec: int) -> Optional[float]:
    """מחיר פתיחת נר 1m שמתחיל ב-window_epoch (Unix שניות). A-7: cached (immutable per epoch)."""
    cached = _OPEN_PRICE_CACHE.get(window_epoch_sec)
    if cached is not None:
        return cached
    start_ms = window_epoch_sec * 1000
    client = _get_binance_client()
    r = await client.get(
        BINANCE_KLINES,
        params={"symbol": "BTCUSDT", "interval": "1m", "startTime": start_ms, "limit": 1},
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    val = float(data[0][1])
    _OPEN_PRICE_CACHE[window_epoch_sec] = val
    _prune_settlement_cache(_OPEN_PRICE_CACHE)
    return val


async def fetch_close_price_at_window_end(
    window_epoch_sec: int,
    window_sec: int,
    *,
    max_retries: int = 3,
    retry_sleep_sec: float = 1.0,
) -> Optional[float]:
    """סגירת נר ה-1m האחרון בטווח [epoch, epoch+window_sec) — מתאים לסוף חלון Up/Down (פרוקסי Binance).

    FIX #4: retry קצר אם Binance עדיין לא פרסם את הנר (נקרא מיד אחרי סגירת החלון).
    הנר נסגר ב-epoch + window_sec; Binance בדרך כלל מפרסם אותו תוך 0.5-2 שניות.
    אנחנו מנסים עד max_retries פעמים עם הפסקה של retry_sleep_sec בין נסיונות.
    """
    import asyncio as _asyncio

    if window_sec < 60:
        return None
    cached = _CLOSE_PRICE_CACHE.get((window_epoch_sec, window_sec))
    if cached is not None:
        return cached
    # הנר האחרון מתחיל ב-epoch + window_sec - 60 ונסגר ב-epoch + window_sec
    last_candle_open_ms = (window_epoch_sec + window_sec - 60) * 1000
    attempt = 0
    while True:
        attempt += 1
        try:
            client = _get_binance_client()
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
            if data:
                # שדה [6] = closeTime במילי-שניות; אם closeTime > now → הנר עדיין פתוח.
                close_time_ms = int(data[0][6]) if len(data[0]) > 6 else 0
                if close_time_ms and close_time_ms > int(time.time() * 1000):
                    # הנר עדיין פתוח — לא לקחת את ה-close שלו (זמני).
                    if attempt < max_retries:
                        await _asyncio.sleep(retry_sleep_sec)
                        continue
                    return None
                # נר סגור — ערך סופי immutable; נשמר ב-cache (רק non-None).
                close_val = float(data[0][4])
                _CLOSE_PRICE_CACHE[(window_epoch_sec, window_sec)] = close_val
                _prune_settlement_cache(_CLOSE_PRICE_CACHE)
                return close_val
        except Exception:
            if attempt >= max_retries:
                raise
        if attempt >= max_retries:
            return None
        await _asyncio.sleep(retry_sleep_sec)


async def fetch_window_start_end_btc_usd(
    window_epoch_sec: int, window_sec: int
) -> dict[str, Any]:
    """
    זוג מחירי התחלה/סוף לפירוק סימולציה (אותה מתודולוגיה כמו price_to_beat).
    הרזולוציה הרשמית ב-Polymarket היא Chainlink — כאן binance_1m_proxy לשקיפות.
    """
    # open ו-close הם נרות עצמאיים — מושכים במקביל (B-6) כדי לחצות את זמן ה-settlement.
    # return_exceptions=False כדי שכשל יתפשט ו-demo_engine יפעיל void-and-refund (לא win/loss שגוי).
    start, end = await asyncio.gather(
        fetch_open_price_at_window_start(window_epoch_sec),
        fetch_close_price_at_window_end(window_epoch_sec, window_sec),
    )
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
