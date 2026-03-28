"""
שכבת CLOB ל-Polymarket — הזמנות BUY/SELL אמיתיות.
דורש: pip install py-clob-client, POLYMARKET_PRIVATE_KEY, ואופציונלי POLYMARKET_SIGNATURE_TYPE / POLYMARKET_FUNDER.
"""
from __future__ import annotations

import os
from typing import Any, Literal, Optional

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
