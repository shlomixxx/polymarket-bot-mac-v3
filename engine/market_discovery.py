"""
גילוי שוק BTC Up/Down — חלון 5 דק׳ או 15 דק׳, rollover אוטומטי.
slug: btc-updown-5m-{epoch} (כל 300s) או btc-updown-15m-{epoch} (כל 900s).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

import httpx

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


async def fetch_event_slug(client: httpx.AsyncClient, slug: str) -> Optional[dict]:
    r = await client.get(f"{GAMMA}/events/slug/{slug}", timeout=15.0)
    if r.status_code != 200:
        return None
    return r.json()


async def discover_active_btc_window(window: BtcWindow = "5m") -> Optional[ActiveMarket]:
    """מוצא את החלון הפעיל לפי סוג השוק (5m / 15m)."""
    step = window_step_sec(window)
    prefix = slug_prefix(window)
    now = int(time.time())
    base = (now // step) * step
    offsets = [0, step, -step, 2 * step, -2 * step, 3 * step, -3 * step]
    async with httpx.AsyncClient() as client:
        for off in offsets:
            epoch = base + off
            slug = f"{prefix}-{epoch}"
            data = await fetch_event_slug(client, slug)
            if not data:
                continue
            am = _parse_event(data)
            if am and not am.closed:
                return am
        for delta in range(-10, 12):
            epoch = base + delta * step
            slug = f"{prefix}-{epoch}"
            data = await fetch_event_slug(client, slug)
            if not data:
                continue
            am = _parse_event(data)
            if am and not am.closed:
                return am
    return None


async def discover_active_btc_5m_window() -> Optional[ActiveMarket]:
    """תאימות לאחור — שוק 5 דק׳."""
    return await discover_active_btc_window("5m")


def seconds_until_window_end(epoch: int, window_sec: int) -> float:
    """סוף חלון = epoch + אורך החלון בשניות."""
    return max(0.0, float(epoch + window_sec - time.time()))


async def get_clob_book(client: httpx.AsyncClient, token_id: str) -> dict[str, Any]:
    r = await client.get(
        "https://clob.polymarket.com/book",
        params={"token_id": token_id},
        timeout=15.0,
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
