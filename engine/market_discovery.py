"""
גילוי שוק BTC Up/Down — חלון 5 דק׳ או 15 דק׳, rollover אוטומטי.
slug: btc-updown-5m-{epoch} (כל 300s) או btc-updown-15m-{epoch} (כל 900s).
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

import httpx

from _cache import SingleFlight

GAMMA = "https://gamma-api.polymarket.com"

STEP_5M = 300
STEP_15M = 900

BtcWindow = Literal["5m", "15m"]


def window_step_sec(window: BtcWindow) -> int:
    return STEP_15M if window == "15m" else STEP_5M


def slug_prefix(window: BtcWindow) -> str:
    return "btc-updown-15m" if window == "15m" else "btc-updown-5m"


@dataclass
class ActiveMarket:
    slug: str
    epoch: int
    condition_id: str
    end_date_iso: str
    closed: bool
    token_up: str
    token_down: str
    outcome_prices: tuple[float, float]
    order_min_size: float
    title: str
    window_sec: int  # 300 או 900 — לאורך החלון בשניות
    # מקור מינימום החוזים: gamma=מטא-דאטה API; clob=מ־GET /book (סמכותי למסחר)
    order_min_size_source: Literal["clob", "gamma"] = "gamma"
    # קישור מקור הרזולוציה מ-Gamma — ללא מחיר מספרי ב-API
    resolution_source: Optional[str] = None


def _parse_event(data: dict[str, Any]) -> Optional[ActiveMarket]:
    if not data or not data.get("markets"):
        return None
    m = data["markets"][0]
    if m.get("closed"):
        return None
    import json

    tokens_raw = m.get("clobTokenIds") or "[]"
    if isinstance(tokens_raw, str):
        tokens = json.loads(tokens_raw)
    else:
        tokens = tokens_raw
    if len(tokens) < 2:
        return None
    prices_raw = m.get("outcomePrices") or "[0,0]"
    if isinstance(prices_raw, str):
        prices = json.loads(prices_raw)
    else:
        prices = prices_raw
    slug = data.get("slug") or m.get("slug") or ""
    epoch = 0
    window_sec = STEP_5M
    if slug.startswith("btc-updown-15m-"):
        window_sec = STEP_15M
        try:
            epoch = int(slug.rsplit("-", 1)[-1])
        except ValueError:
            pass
    elif slug.startswith("btc-updown-5m-"):
        window_sec = STEP_5M
        try:
            epoch = int(slug.rsplit("-", 1)[-1])
        except ValueError:
            pass
    return ActiveMarket(
        slug=slug,
        epoch=epoch,
        condition_id=m.get("conditionId", ""),
        end_date_iso=m.get("endDate", ""),
        closed=bool(m.get("closed")),
        token_up=str(tokens[0]),
        token_down=str(tokens[1]),
        outcome_prices=(float(prices[0]), float(prices[1]) if len(prices) > 1 else 0.0),
        order_min_size=float(m.get("orderMinSize") or 5),
        title=data.get("title") or m.get("question") or "",
        window_sec=window_sec,
        resolution_source=(m.get("resolutionSource") or None) if isinstance(m.get("resolutionSource"), str) else None,
    )


# Cache של min_order_size מ־CLOB לפי token_id, כדי לא להיכנס ל־CLOB בכל discovery.
# המינימום של חוזה לא משתנה במהלך חייו של החלון — TTL ארוך מספיק.
_CLOB_MIN_SIZE_CACHE: dict[str, tuple[float, float, str]] = {}  # token_id -> (ts, value, source)
_CLOB_MIN_SIZE_TTL_SEC = 120.0


def _cached_clob_min_size(token_id: str) -> Optional[tuple[float, str]]:
    entry = _CLOB_MIN_SIZE_CACHE.get(token_id)
    if not entry:
        return None
    ts, val, source = entry
    if (time.time() - ts) > _CLOB_MIN_SIZE_TTL_SEC:
        return None
    return (val, source)


async def apply_clob_order_min_size(
    am: ActiveMarket,
    client: httpx.AsyncClient,
    *,
    timeout: float = 3.0,
) -> None:
    """מעדכן את order_min_size מתשובת CLOB ‎/book‎ (מינימום אמיתי להזמנה). Gamma עלול להסתדר אחרת.
    משתמש ב־cache לפי token_id (TTL 120s) כדי לא להעמיס על CLOB בכל discovery."""
    cached = _cached_clob_min_size(am.token_up)
    if cached is not None:
        v, source = cached
        am.order_min_size = v
        am.order_min_size_source = source  # type: ignore[assignment]
        return
    try:
        book = await asyncio.wait_for(get_clob_book(client, am.token_up), timeout=timeout)
        raw = book.get("min_order_size")
        if raw is None:
            return
        v = float(raw)
        if not math.isfinite(v) or v <= 0:
            return
        am.order_min_size = v
        am.order_min_size_source = "clob"
        _CLOB_MIN_SIZE_CACHE[am.token_up] = (time.time(), v, "clob")
    except Exception:
        pass


async def fetch_event_slug(client: httpx.AsyncClient, slug: str) -> Optional[dict]:
    r = await client.get(f"{GAMMA}/events/slug/{slug}", timeout=4.0)
    if r.status_code != 200:
        return None
    return r.json()


_DISCOVERY_CACHE: dict[BtcWindow, tuple[float, ActiveMarket]] = {}
_DISCOVERY_LOCKS: dict[BtcWindow, asyncio.Lock] = {}
_DISCOVERY_TTL_SEC = 30.0


def _cached_market(window: BtcWindow) -> Optional[ActiveMarket]:
    # A-5: ה-slug/epoch/tokens/window_sec הם פונקציה דטרמיניסטית של ה-epoch — immutable לכל אורך
    # חיי החלון. לכן מחזיקים את השוק לכל גוף החלון (אין יותר תפוגת TTL שטוחה של 30s) — חוסך
    # ~90% קריאות Gamma. שומרים את תפוגת הגבול: ברגע שהחלון נסגר -> None -> re-discovery ל-epoch
    # החדש (קריטי ל-rollover; ה-warmer מאיץ ל-1.5s סביב הגבול). outcome_prices (תצוגה גסה ב-
    # /api/market/current בלבד) מתרענן פעם בחלון — המחיר החי מגיע מ-WS/CLOB, לא מכאן.
    entry = _DISCOVERY_CACHE.get(window)
    if not entry:
        return None
    _ts, am = entry
    if seconds_until_window_end(am.epoch, am.window_sec) <= 0:
        return None
    return am


def _stale_market_if_window_open(window: BtcWindow) -> Optional[ActiveMarket]:
    """מחזיר את הקאש האחרון (גם אם פג ה־TTL) כל עוד החלון עצמו עדיין פתוח.
    משמש כ־fallback כשפנייה ל־Gamma מאטה מעבר לטיים־אאוט — עדיף על שגיאה ל־UI."""
    entry = _DISCOVERY_CACHE.get(window)
    if not entry:
        return None
    _ts, am = entry
    if seconds_until_window_end(am.epoch, am.window_sec) <= 0:
        return None
    return am


_DISCOVERY_CLIENT: Optional[httpx.AsyncClient] = None


def _get_discovery_client() -> httpx.AsyncClient:
    global _DISCOVERY_CLIENT
    if _DISCOVERY_CLIENT is None:
        _DISCOVERY_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=3.0, write=3.0, pool=3.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _DISCOVERY_CLIENT


async def _discover_uncached(window: BtcWindow) -> Optional[ActiveMarket]:
    step = window_step_sec(window)
    prefix = slug_prefix(window)
    now = int(time.time())
    base = (now // step) * step
    # קודם החלון הנוכחי. אם נמצא — חוזרים מיד; אחרת בודקים 4 קרובים במקביל.
    client = _get_discovery_client()
    am = await _try_epoch(client, prefix, base)
    if am is not None:
        return am
    candidate_epochs = [base + step, base - step, base + 2 * step, base - 2 * step]
    results = await asyncio.gather(
        *(_try_epoch(client, prefix, e) for e in candidate_epochs),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, ActiveMarket):
            return r
    return None


async def _try_epoch(client: httpx.AsyncClient, prefix: str, epoch: int) -> Optional[ActiveMarket]:
    """גילוי שוק לפי epoch בודד. אם ב־cache של CLOB יש כבר min_order_size — נשתמש בו מיד.
    אחרת מעדכנים מ־CLOB ברקע (לא חוסמים את ה־hot path) כדי לקצר את ה־latency של הקריאה."""
    slug = f"{prefix}-{epoch}"
    try:
        data = await fetch_event_slug(client, slug)
    except Exception:
        return None
    if not data:
        return None
    am = _parse_event(data)
    if am and not am.closed:
        cached = _cached_clob_min_size(am.token_up)
        if cached is not None:
            v, source = cached
            am.order_min_size = v
            am.order_min_size_source = source  # type: ignore[assignment]
        else:
            # שדרוג ברקע — לא חוסם את הקריאה הראשונה
            asyncio.create_task(_refresh_clob_min_size_background(am.token_up))
        return am
    return None


async def _refresh_clob_min_size_background(token_id: str) -> None:
    """רענון min_order_size מ־CLOB ברקע — שומר ב־cache לקריאות הבאות."""
    try:
        client = _get_discovery_client()
        book = await asyncio.wait_for(get_clob_book(client, token_id), timeout=4.0)
        raw = book.get("min_order_size")
        if raw is None:
            return
        v = float(raw)
        if not math.isfinite(v) or v <= 0:
            return
        _CLOB_MIN_SIZE_CACHE[token_id] = (time.time(), v, "clob")
    except Exception:
        pass


async def discover_active_btc_window(window: BtcWindow = "5m") -> Optional[ActiveMarket]:
    """מוצא את החלון הפעיל לפי סוג השוק (5m / 15m). קאש 30 שניות + תקרת זמן 8s למניעת timeouts.
    כשהפנייה ל־Gamma מאטה: מחזירים את הקאש האחרון אם החלון עדיין פתוח (stale-on-error),
    כדי שלא ייתקעו ה־UI/loop על 504 בזמן עומס נקודתי בצד של Polymarket."""
    cached = _cached_market(window)
    if cached is not None:
        return cached
    lock = _DISCOVERY_LOCKS.setdefault(window, asyncio.Lock())
    async with lock:
        cached = _cached_market(window)
        if cached is not None:
            return cached
        try:
            am = await asyncio.wait_for(_discover_uncached(window), timeout=8.0)
        except asyncio.TimeoutError:
            am = None
        if am is not None:
            _DISCOVERY_CACHE[window] = (time.time(), am)
            return am
        # Stale fallback — נחזיר את הערך האחרון בקאש כל עוד החלון עוד פתוח,
        # כדי לא להחזיר 504 ל־UI כשהיא רק תקלת רשת זמנית מול Gamma.
        return _stale_market_if_window_open(window)


async def discover_active_btc_5m_window() -> Optional[ActiveMarket]:
    """תאימות לאחור — שוק 5 דק׳."""
    return await discover_active_btc_window("5m")


async def discovery_warmer_loop(
    window_getter,
    *,
    interval_sec: float = 10.0,
    rollover_grace_sec: float = 12.0,
    rollover_interval_sec: float = 1.5,
) -> None:
    """לולאת רענון רקע שמרעננת את הקאש של גילוי השוק לפני שפג ה־TTL.
    מטרה: גם כשפניות ל־Gamma איטיות, המשתמש לא מרגיש בעיכוב כי הקאש תמיד טרי.
    `window_getter` הוא callable שמחזיר את ה־btc_window הנוכחי (5m / 15m).

    מצב מיוחד: סביב סוף חלון (`rollover_grace_sec` שניות אחרונות + מיד אחרי
    סגירה) מורידים את התדירות ל־`rollover_interval_sec` כדי לתפוס את ה־epoch
    הבא מ־Gamma מהר ולמנוע "חלון מת" שבו ה־UI לא יודע איזה שוק להציג.

    כשלים בלולאה לעולם לא מקפיצים חריגה — הם נבלעים, כי זו עבודת רקע.
    """
    await asyncio.sleep(0.5)
    while True:
        try:
            window = window_getter()
            if window not in ("5m", "15m"):
                window = "5m"
            await discover_active_btc_window(window)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        # אם אנחנו קרובים ל־rollover — נריץ refresh בתדירות גבוהה יותר עד שנתפוס epoch חדש
        sleep_for = interval_sec
        try:
            entry = _DISCOVERY_CACHE.get(window)  # type: ignore[arg-type]
            if entry is not None:
                _ts, am = entry
                left = seconds_until_window_end(am.epoch, am.window_sec)
                if left <= rollover_grace_sec:
                    sleep_for = rollover_interval_sec
        except Exception:
            pass
        await asyncio.sleep(sleep_for)


def seconds_until_window_end(epoch: int, window_sec: int) -> float:
    """סוף חלון = epoch + אורך החלון בשניות."""
    return max(0.0, float(epoch + window_sec - time.time()))


def peek_window_timing_for_ui(window: BtcWindow) -> Optional[dict[str, Any]]:
    """מספרי חלון ל־UI בפול קל (/api/demo/snapshot) — בלי HTTP.
    משתמש בקאש הגילוי גם אחרי TTL כל עוד אותו חלון עדיין פתוח (seconds_left > 0)."""
    entry = _DISCOVERY_CACHE.get(window)
    if not entry:
        return None
    _ts, am = entry
    sl = seconds_until_window_end(am.epoch, am.window_sec)
    if sl <= 0:
        return None
    return {
        "slug": am.slug,
        "epoch": am.epoch,
        "window_sec": am.window_sec,
        "seconds_left": int(sl),
        "btc_window": window,
    }


# B-3: single-flight — קריאות *מקבילות* לאותו token חולקות בקשת רשת אחת. אין cache מבוסס-זמן,
# אז אין staleness: כל caller מקבל תוצאה טרייה. מחיר ה-fill בפועל (demo_engine.best_ask) הוא
# פונקציה נפרדת שמושכת חי תמיד — לא מושפע.
_BOOK_SINGLE_FLIGHT = SingleFlight()


async def get_clob_book(client: httpx.AsyncClient, token_id: str) -> dict[str, Any]:
    return await _BOOK_SINGLE_FLIGHT.do(token_id, lambda: _get_clob_book_uncached(client, token_id))


async def _get_clob_book_uncached(client: httpx.AsyncClient, token_id: str) -> dict[str, Any]:
    r = await client.get(
        "https://clob.polymarket.com/book",
        params={"token_id": token_id},
        timeout=6.0,
    )
    r.raise_for_status()
    data = r.json()

    # חשוב: ה-API מחזיר רמות לא בהכרח ממויינות (ראינו bid=0.01 ראשון).
    bids = list(data.get("bids") or [])
    asks = list(data.get("asks") or [])
    try:
        bids.sort(key=lambda x: float(x["price"]), reverse=True)
        asks.sort(key=lambda x: float(x["price"]))
        data["bids"] = bids
        data["asks"] = asks
    except Exception:
        pass
    return data
