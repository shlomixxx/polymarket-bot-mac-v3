"""
שכבת CLOB ל-Polymarket — הזמנות BUY/SELL אמיתיות.
דורש: pip install py-clob-client, POLYMARKET_PRIVATE_KEY, ואופציונלי POLYMARKET_SIGNATURE_TYPE / POLYMARKET_FUNDER.
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable, Literal, Optional

import httpx

POLYMARKET_DATA_API = "https://data-api.polymarket.com"

SideName = Literal["BUY", "SELL"]


def _live_disabled_reason() -> Optional[str]:
    """POLYMARKET_LIVE=0 — kill switch; אחרת דורש מפתח."""
    v = os.environ.get("POLYMARKET_LIVE", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return "מסחר לייב כבוי (POLYMARKET_LIVE=0)"
    pk = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    if not pk:
        return "חסר POLYMARKET_PRIVATE_KEY"
    return None


def build_trading_client():
    """מחזיר (client, None) או (None, error_message)."""
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        return None, "התקן py-clob-client: pip install py-clob-client"

    err = _live_disabled_reason()
    if err:
        return None, err

    pk = os.environ["POLYMARKET_PRIVATE_KEY"].strip()
    host = "https://clob.polymarket.com"
    chain_id = 137

    temp = ClobClient(host, chain_id=chain_id, key=pk)
    try:
        creds = temp.create_or_derive_api_creds()
    except Exception as e:
        return None, f"API credentials: {e}"

    sig_raw = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0").strip()
    try:
        signature_type = int(sig_raw)
    except ValueError:
        signature_type = 0

    funder = (os.environ.get("POLYMARKET_FUNDER") or "").strip()
    if not funder:
        try:
            funder = temp.get_address()
        except Exception:
            return None, "הגדר POLYMARKET_FUNDER או וודא מפתח תקין"

    client = ClobClient(
        host,
        chain_id=chain_id,
        key=pk,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
    )
    return client, None


async def place_limit_order(
    token_id: str,
    price: float,
    size: float,
    side: SideName,
) -> dict[str, Any]:
    """
    שולח הזמנת GTC. מחזיר dict עם ok, order_id או error.
    """
    try:
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL
    except ImportError:
        return {"ok": False, "error": "חסר py-clob-client"}

    client, err = build_trading_client()
    if err:
        return {"ok": False, "error": err}

    try:
        tick_size = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
    except Exception as e:
        return {"ok": False, "error": f"שוק/טוקן: {e}"}

    opts = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    side_const = BUY if side == "BUY" else SELL

    try:
        resp = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side_const,
            ),
            opts,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    oid = None
    if isinstance(resp, dict):
        oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    else:
        oid = str(resp)
    return {"ok": True, "order_id": oid, "raw": resp, "price": float(price), "size": float(size)}


def live_trading_enabled() -> bool:
    return _live_disabled_reason() is None


def _normalize_usdc_amount(val: Any) -> Optional[float]:
    """ממיר תגובת CLOB (לרוב micro-USDC, 6 עשרוניות) לדולרים."""
    if val is None:
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if x == 0:
        return 0.0
    # ה-API מחזיר לעיתים מספר שלם ב-micro-USDC (למשל 5_000_000 = 5$)
    if abs(x) >= 1e6:
        x = x / 1e6
    return round(float(x), 4)


def fetch_polymarket_clob_account() -> dict[str, Any]:
    """
    יתרת USDC (collateral) ו-allowance כפי שה-CLOB של Polymarket רואה — לא כל תיק האתר,
    אלא מה שמקושר לחשבון המסחר דרך המפתח הנוכחי.
    """
    client, err = build_trading_client()
    if err:
        return {"ok": False, "error": err}

    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError:
        return {"ok": False, "error": "חסר py-clob-client"}

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    try:
        raw = client.get_balance_allowance(params)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    bal_usd = None
    allow_usd = None
    if isinstance(raw, dict):
        bal_usd = _normalize_usdc_amount(
            raw.get("balance") if raw.get("balance") is not None else raw.get("Balance"),
        )
        allow_usd = _normalize_usdc_amount(
            raw.get("allowance") if raw.get("allowance") is not None else raw.get("Allowance"),
        )
    addr = None
    try:
        addr = client.get_address()
    except Exception:
        pass

    return {
        "ok": True,
        "balance_usd": bal_usd,
        "allowance_usd": allow_usd,
        "address": addr,
        "raw": raw if isinstance(raw, dict) else {"response": raw},
    }


def _infer_side(raw: dict[str, Any]) -> str:
    """מנחש Up/Down מתוך רשומת פוזיציה של Polymarket Data API (שם outcome)."""
    outcome = str(raw.get("outcome") or raw.get("outcomeName") or raw.get("title") or "").strip()
    norm = outcome.lower()
    if norm in ("up", "yes", "higher", "long"):
        return "Up"
    if norm in ("down", "no", "lower", "short"):
        return "Down"
    # ברירת מחדל: "Up" אם לא ידוע. הצרכן תמיד יכול להצליב מול state פנימי.
    return outcome or "Up"


def _pick_float(d: dict[str, Any], *keys: str) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv:  # NaN check
            return fv
    return None


def _normalize_position_record(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """ממיר רשומה מ-data-api.polymarket.com/positions לשם-שדות שלנו."""
    tok = raw.get("asset") or raw.get("tokenId") or raw.get("token_id") or raw.get("tokenID")
    if tok is None:
        return None
    size = _pick_float(raw, "size", "amount", "shares", "balance")
    if size is None or size <= 0:
        return None
    avg = _pick_float(raw, "avgPrice", "avg_price", "averagePrice", "entryPrice")
    mark = _pick_float(raw, "curPrice", "currentPrice", "lastPrice", "markPrice")
    value = _pick_float(raw, "currentValue", "value", "valueUsd")
    if value is None and mark is not None:
        value = size * mark
    return {
        "token_id": str(tok),
        "side": _infer_side(raw),
        "size": float(size),
        "avg_price": float(avg) if avg is not None else None,
        "mark_price": float(mark) if mark is not None else None,
        "value_usd": float(value) if value is not None else None,
        "raw": raw,
    }


async def fetch_live_positions(address: str, *, client: Optional[httpx.AsyncClient] = None) -> list[dict[str, Any]]:
    """
    שולף פוזיציות פתוחות לחשבון מ-Polymarket Data API.
    לא דורש מפתח — ציבורי לפי כתובת. מחזיר רשימה מנורמלת (אולי ריקה) גם בשגיאה.
    """
    if not address:
        return []
    url = f"{POLYMARKET_DATA_API}/positions"
    params = {"user": address, "sizeThreshold": 0.001}
    owns_client = client is None
    cl = client or httpx.AsyncClient(timeout=8.0)
    try:
        resp = await cl.get(url, params=params)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []
    finally:
        if owns_client:
            await cl.aclose()

    if isinstance(data, dict):
        rows: Iterable[Any] = data.get("positions") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        norm = _normalize_position_record(r)
        if norm:
            out.append(norm)
    return out


_PORTFOLIO_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
_PORTFOLIO_CACHE_TTL_SEC = 2.0


async def fetch_live_portfolio(*, force: bool = False) -> dict[str, Any]:
    """
    מאחד יתרת USDC מ-CLOB + פוזיציות פתוחות מה-Data API של Polymarket ל-snapshot יחיד לממשק.
    עם cache קצר (2 שניות) כדי לא לחנוק את ה-API כש-UI מזמן תכופות.
    """
    now = time.time()
    cached = _PORTFOLIO_CACHE.get("payload")
    if not force and cached and now - float(_PORTFOLIO_CACHE.get("ts") or 0) < _PORTFOLIO_CACHE_TTL_SEC:
        return cached  # type: ignore[return-value]

    acct = fetch_polymarket_clob_account()
    if not acct.get("ok"):
        payload = {
            "ok": False,
            "error": acct.get("error", "לא ניתן לקרוא ל-CLOB"),
            "address": acct.get("address"),
            "balance_usd": None,
            "allowance_usd": None,
            "positions": [],
            "equity_usd": None,
            "ts": now,
        }
        _PORTFOLIO_CACHE["ts"] = now
        _PORTFOLIO_CACHE["payload"] = payload
        return payload

    address = acct.get("address") or ""
    positions = await fetch_live_positions(address)
    balance_usd = acct.get("balance_usd")
    total_pos_value = 0.0
    for p in positions:
        v = p.get("value_usd")
        if isinstance(v, (int, float)):
            total_pos_value += float(v)
    equity_usd: Optional[float] = None
    if isinstance(balance_usd, (int, float)):
        equity_usd = float(balance_usd) + total_pos_value

    payload = {
        "ok": True,
        "address": address or None,
        "balance_usd": balance_usd,
        "allowance_usd": acct.get("allowance_usd"),
        "positions": positions,
        "equity_usd": equity_usd,
        "ts": now,
    }
    _PORTFOLIO_CACHE["ts"] = now
    _PORTFOLIO_CACHE["payload"] = payload
    return payload


def reset_portfolio_cache() -> None:
    _PORTFOLIO_CACHE["ts"] = 0.0
    _PORTFOLIO_CACHE["payload"] = None
