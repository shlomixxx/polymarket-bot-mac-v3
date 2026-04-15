"""
שכבת CLOB ל-Polymarket — הזמנות BUY/SELL אמיתיות.
דורש: pip install py-clob-client, POLYMARKET_PRIVATE_KEY, ואופציונלי POLYMARKET_SIGNATURE_TYPE / POLYMARKET_FUNDER.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Iterable, Literal, Optional

import httpx

POLYMARKET_DATA_API = "https://data-api.polymarket.com"

SideName = Literal["BUY", "SELL"]
OrderModeName = Literal["limit", "market"]
MarketOrderTypeName = Literal["FOK", "FAK"]


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
    signer_addr: Optional[str] = None
    try:
        signer_addr = temp.get_address()
    except Exception:
        pass

    if not funder:
        if not signer_addr:
            return None, "הגדר POLYMARKET_FUNDER או וודא מפתח תקין"
        funder = signer_addr

    # proxy (1 / 2): ה-funder חייב להיות כתובת ה-proxy שבה הכספים — לא ה-EOA. אחרת ההזמנות נכשלות ב-invalid signature.
    if signature_type in (1, 2) and signer_addr and funder.lower() == signer_addr.lower():
        return None, (
            "עבור POLYMARKET_SIGNATURE_TYPE=1 או 2 חובה להגדיר POLYMARKET_FUNDER=<כתובת proxy> "
            "מחשבון Polymarket (באתר: Profile / Settings — כתובת Deposit / ארנק המסחר). "
            "לא להשתמש בכתובת החותם (EOA) כ-funder — זה גורם ל־invalid signature בהזמנות."
        )

    client = ClobClient(
        host,
        chain_id=chain_id,
        key=pk,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
    )
    return client, None


def check_balance_before_order(required_usd: float) -> tuple[bool, Optional[str]]:
    """
    בודק שיתרת CLOB מספיקה לפני שליחת הזמנה.
    מחזיר (True, None) אם בסדר, או (False, הודעת_שגיאה) אם לא.
    """
    acct = fetch_polymarket_clob_account()
    if not acct.get("ok"):
        return False, f"לא ניתן לבדוק יתרה: {acct.get('error', 'שגיאה לא ידועה')}"

    balance = acct.get("balance_usd")
    if balance is None:
        return False, "לא ניתן לקרוא יתרת CLOB — בדקו הגדרות מפתח וחיבור."

    if balance < required_usd:
        msg = (
            f"יתרת CLOB: ${balance:.2f}, נדרש ~${required_usd:.2f}. "
        )
        if balance == 0:
            sig_type = 0
            try:
                sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0").strip() or "0")
            except ValueError:
                pass
            if sig_type == 0:
                msg += (
                    "יתרה 0 — אם ייצאת מפתח מארנק Polymarket, הגדר "
                    "POLYMARKET_SIGNATURE_TYPE=1 (Poly proxy) או 2 (Gnosis Safe) "
                    "ו-POLYMARKET_FUNDER=<כתובת proxy>. ראו docs.polymarket.com — CLOB quickstart. "
                )
            msg += (
                "יש להפקיד USDC לחשבון CLOB ב-Polymarket "
                "(Deposit דרך polymarket.com — USDC בארנק Polygon לבד לא מספיק)."
            )
        else:
            msg += "יש להפקיד עוד USDC לחשבון CLOB ב-Polymarket."
        return False, msg

    return True, None


def live_trading_enabled() -> bool:
    return _live_disabled_reason() is None


def _normalize_usdc_amount(val: Any) -> Optional[float]:
    """ממיר תגובת CLOB (micro-USDC, 6 עשרוניות לדולר) לדולרים.

    לפי תיעוד Polymarket/py-clob-client הערכים הם ביחידות הקטנות (1e6 = 1 USDC).
    סף כפול: ערכים ≥1e6 בוודאות micro; בין 1e4 ל-1e6 — micro גם כשהסכום < 1$
    (למשל 500_000 = 0.50$). ערכים עם שבר עשרוני וקטנים מ-1e4 — נחשבים כדולרים כבר מנורמלים.
    """
    if val is None:
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if x == 0:
        return 0.0
    ax = abs(x)
    frac = abs(x - round(x)) > 1e-9
    if ax >= 1e6:
        x = x / 1e6
    elif not frac and ax >= 1e4:
        # מיקרו מתחת לדולר אחד (ולא מעל 1e6) — לא לפספס 500_000 → 0.50$
        x = x / 1e6
    return round(float(x), 4)


def _fetch_conditional_balance_shares(client: Any, token_id: str) -> Optional[float]:
    """יתרת חוזי תוצאה (CONDITIONAL) ב-CLOB — 6 עשרוניות מיקרו תמיד.

    ה-CLOB מחזיר את ה-balance כמספר שלם גולמי (כמו 4083) שמייצג 0.004083 חוזים.
    לא משתמש ב-_normalize_usdc_amount כי ההיוריסטיקה שלו (סף 1e4) מחמיצה יתרות
    קטנות (<$0.01) ומחזירה אותן ללא חילוק — קריטי לזיהוי "חוזי רפאים" אחרי
    mill'oy חלקי של GTC.
    """
    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        pcond = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(token_id))
        raw = client.get_balance_allowance(pcond)
        if not isinstance(raw, dict):
            return None
        bal = raw.get("balance") if raw.get("balance") is not None else raw.get("Balance")
        if bal is None:
            return None
        try:
            x = float(bal)
        except (TypeError, ValueError):
            return None
        return round(x / 1e6, 6)
    except Exception:
        return None


async def fetch_chain_shares_for_token(token_id: str) -> Optional[float]:
    """מחזיר את יתרת חוזי התוצאה בשרשרת לטוקן נתון (Polymarket CLOB), או None אם לא זמין.

    מקור־אמת אמיתי (bypass ל-Data API שעלול להיות מאחר). משמש לסנכרון
    p.contracts מיידי כשמקבלים insufficient_onchain_balance, כדי לא להסתמך
    על reconcile שעלול להחזיר ערך מיושן או אפילו לנפח חזרה את הפוזיציה."""
    def _sync() -> Optional[float]:
        cl, err = build_trading_client()
        if err or cl is None:
            return None
        return _fetch_conditional_balance_shares(cl, str(token_id))
    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        return None


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

    # בדיקת יתרה מראש — הודעה ברורה במקום שגיאת CLOB קריפטית
    if side == "BUY":
        required = float(price) * float(size)
        bal_ok, bal_err = check_balance_before_order(required)
        if not bal_ok:
            return {"ok": False, "error": bal_err}

    try:
        tick_size = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
    except Exception as e:
        return {"ok": False, "error": f"שוק/טוקן: {e}"}

    order_size = float(size)
    # SELL: ה-CLOB בודק יתרת טוקן תנאי בשרשרת — היומן הפנימי יכול להראות יותר חוזים ממה שבפועל (אחרי reconcile חלקי).
    if side == "SELL":
        avail = _fetch_conditional_balance_shares(client, str(token_id))
        if avail is not None and order_size > avail + 1e-9:
            capped = max(0.0, min(order_size, avail * (1.0 - 1e-8)))
            if capped < 1e-8:
                return {
                    "ok": False,
                    "error": (
                        f"אין מספיק יתרת טוקן ב-CLOB למכירה: זמין ~{avail:.4f} חוזים, "
                        f"ביקשת {order_size:.4f}. המתן ל-reconcile או רענן."
                    ),
                }
            order_size = float(f"{capped:.6f}")

    opts = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    side_const = BUY if side == "BUY" else SELL

    try:
        resp = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(order_size),
                side=side_const,
            ),
            opts,
        )
    except Exception as e:
        err = str(e)
        low = err.lower()
        err_code: Optional[str] = None
        if "invalid signature" in low:
            err += (
                " — בדקו: POLYMARKET_FUNDER=כתובת ה-proxy מהאתר (לא EOA); "
                "נסו POLYMARKET_SIGNATURE_TYPE=2 אם החשבון דרך Gnosis Safe; "
                "המפתח חייב להיות של אותו חשבון Polymarket ששימש לייצוא."
            )
        if "not enough balance" in low or "not enough balance / allowance" in low:
            err += (
                " — ב־SELL: ייתכן פחות חוזי תוצאה בשרשרת מבספר הפוזיציה הפנימי; "
                "המערכת אמורה לכווץ את ההזמנה לפי יתרת ה-CLOB — נסו שוב אחרי reconcile."
            )
            err_code = "insufficient_onchain_balance"
        out: dict[str, Any] = {"ok": False, "error": err}
        if err_code:
            out["error_code"] = err_code
        return out

    oid = None
    if isinstance(resp, dict):
        oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    else:
        oid = str(resp)
    return {"ok": True, "order_id": oid, "raw": resp, "price": float(price), "size": float(order_size)}


def _clamp_slippage_price(price: float, side: SideName, slippage_pct: float) -> float:
    """
    ממיר slippage_pct לתקרת מחיר חוקית עבור MarketOrderArgs.price:
    - BUY: המחיר הכי גרוע = price * (1 + slip%), מוגבל ל-0.999
    - SELL: המחיר הכי גרוע (הנמוך ביותר שנקבל) = price * (1 - slip%), מוגבל ל-0.001
    """
    p = float(price)
    s = max(0.0, float(slippage_pct)) / 100.0
    if side == "BUY":
        return min(0.999, max(0.001, p * (1.0 + s)))
    return min(0.999, max(0.001, p * (1.0 - s)))


async def place_market_order(
    token_id: str,
    amount: float,
    side: SideName,
    *,
    order_type: MarketOrderTypeName = "FOK",
    slippage_cap_price: Optional[float] = None,
) -> dict[str, Any]:
    """
    Market order דרך MarketOrderArgs + create_market_order + post_order(orderType=FOK|FAK).

    - BUY: amount = **דולרים** (Polymarket ימיר לחוזים לפי ה-book).
    - SELL: amount = **חוזים**.
    - FOK: או הכל או כלום (לכניסה — אין חצי פוזיציה).
    - FAK: סוגר כמה שאפשר, מבטל שארית (ליציאה).
    - slippage_cap_price: המחיר הגרוע ביותר שמוכן לקבל (0..1). None = לא מגביל.
    """
    try:
        from py_clob_client.clob_types import (
            MarketOrderArgs,
            OrderType,
            PartialCreateOrderOptions,
        )
        from py_clob_client.order_builder.constants import BUY, SELL
    except ImportError:
        return {"ok": False, "error": "חסר py-clob-client (נסה: pip install -U py-clob-client)"}

    client, err = build_trading_client()
    if err:
        return {"ok": False, "error": err}

    amt = float(amount)
    if amt <= 0:
        return {"ok": False, "error": f"amount לא חיובי ({amt})"}

    if side == "BUY":
        # amount כבר בדולרים. תוספת 1% שוליים לעמלות/עיגול.
        bal_ok, bal_err = check_balance_before_order(amt * 1.01)
        if not bal_ok:
            return {"ok": False, "error": bal_err}

    try:
        tick_size = client.get_tick_size(token_id)
        neg_risk = client.get_neg_risk(token_id)
    except Exception as e:
        return {"ok": False, "error": f"שוק/טוקן: {e}"}

    use_amount = amt
    if side == "SELL":
        avail = _fetch_conditional_balance_shares(client, str(token_id))
        if avail is not None and use_amount > avail + 1e-9:
            capped = max(0.0, min(use_amount, avail * (1.0 - 1e-8)))
            if capped < 1e-8:
                return {
                    "ok": False,
                    "error": (
                        f"אין מספיק יתרת טוקן ב-CLOB למכירה: זמין ~{avail:.4f} חוזים, "
                        f"ביקשת {use_amount:.4f}"
                    ),
                }
            use_amount = float(f"{capped:.6f}")

    side_const = BUY if side == "BUY" else SELL
    ot_const = OrderType.FAK if str(order_type).upper() == "FAK" else OrderType.FOK

    args_kwargs: dict[str, Any] = {
        "token_id": str(token_id),
        "amount": float(use_amount),
        "side": side_const,
        "order_type": ot_const,
    }
    if slippage_cap_price is not None:
        sc = float(slippage_cap_price)
        if sc > 0:
            args_kwargs["price"] = min(0.999, max(0.001, sc))
    args = MarketOrderArgs(**args_kwargs)

    opts = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
    try:
        signed = client.create_market_order(args, opts)
    except Exception as e:
        return {"ok": False, "error": f"create_market_order: {e}"}

    try:
        resp = client.post_order(signed, orderType=ot_const)
    except Exception as e:
        err = str(e)
        low = err.lower()
        err_code: Optional[str] = None
        if "invalid signature" in low:
            err += (
                " — בדקו: POLYMARKET_FUNDER=כתובת ה-proxy מהאתר (לא EOA); "
                "נסו POLYMARKET_SIGNATURE_TYPE=2 אם החשבון דרך Gnosis Safe."
            )
        if "not enough balance" in low:
            err += " — יתרה חסרה (USDC ל-BUY או חוזים ל-SELL)."
            err_code = "insufficient_onchain_balance"
        if "min" in low and "size" in low:
            err += " — מתחת למינימום השוק (order_min_size)."
            err_code = err_code or "min_order_size"
        out: dict[str, Any] = {"ok": False, "error": err}
        if err_code:
            out["error_code"] = err_code
        return out

    oid = None
    matched: Optional[float] = None
    if isinstance(resp, dict):
        oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        # 'matched' מכאן והלאה תמיד מבוטא ב-CHOCES (shares), לא ב-USDC, גם ל-SELL וגם ל-BUY.
        # Polymarket מחזיר:
        #   - SELL (taker): makingAmount = חוזים שמסרנו, takingAmount = USDC שקיבלנו
        #   - BUY  (taker): makingAmount = USDC ששילמנו,   takingAmount = חוזים שקיבלנו
        # עד לתיקון הזה, הקוד לקח תמיד takingAmount — דבר שחישב SELL בדולרים
        # (1 share * 0.64 = 0.64 USDC) ולא בחוזים, וגרם לפער־ספר (phantom contracts).
        if side == "SELL":
            matched_raw = resp.get("makingAmount")
            if matched_raw is None:
                matched_raw = resp.get("size_matched") or resp.get("matched")
        else:
            matched_raw = resp.get("takingAmount")
            if matched_raw is None:
                matched_raw = resp.get("size_matched") or resp.get("matched")
        if matched_raw is not None:
            try:
                matched = float(matched_raw)
            except (TypeError, ValueError):
                matched = None
    else:
        oid = str(resp)

    return {
        "ok": True,
        "order_id": oid,
        "raw": resp,
        "side": side,
        "order_type": str(order_type),
        "amount": float(use_amount),
        "matched": matched,
    }


async def _retry_market_sell_ladder(
    token_id: str,
    remaining_contracts: float,
    *,
    bid: float,
    base_slippage_pct: float,
    max_attempts: int = 3,
    widen_factor: float = 1.5,
    jitter_sec: tuple[float, float] = (0.15, 0.4),
) -> dict[str, Any]:
    """
    Retry ladder ליציאה: אחרי FAK חלקי, מנסה עד max_attempts פעמים עם slippage מתרחב.
    מחזיר dict מצטבר: {ok, sold_total_contracts, attempts, last_error?}.
    """
    sold_total = 0.0
    last_error: Optional[str] = None
    attempts_log: list[dict[str, Any]] = []

    remaining = float(remaining_contracts)
    slip_pct = float(base_slippage_pct)

    for attempt in range(1, max_attempts + 1):
        if remaining <= 1e-8:
            break
        slip_price = _clamp_slippage_price(bid, "SELL", slip_pct)
        r = await place_market_order(
            token_id,
            remaining,
            "SELL",
            order_type="FAK",
            slippage_cap_price=slip_price,
        )
        attempts_log.append({
            "attempt": attempt,
            "slip_pct": slip_pct,
            "slip_price": slip_price,
            "ok": bool(r.get("ok")),
            "matched": r.get("matched"),
            "error": r.get("error"),
        })
        if not r.get("ok"):
            last_error = str(r.get("error") or "")
            low = last_error.lower()
            if "min" in low and "size" in low:
                # שארית < min_order_size — לא ניתן למכור יותר דרך CLOB
                break
            # Rate limit / transient — exponential backoff עם jitter
            await asyncio.sleep(random.uniform(*jitter_sec) * (2 ** (attempt - 1)))
            slip_pct *= widen_factor
            continue

        matched = r.get("matched")
        try:
            matched_n = float(matched) if matched is not None else 0.0
        except (TypeError, ValueError):
            matched_n = 0.0
        sold_total += matched_n
        remaining = max(0.0, remaining - matched_n)
        if remaining <= 1e-8:
            break
        # חלקי — מרחיבים slippage ונסים שוב
        slip_pct *= widen_factor
        await asyncio.sleep(random.uniform(*jitter_sec))

    return {
        "ok": sold_total > 0,
        "sold_total_contracts": float(sold_total),
        "remaining_contracts": float(remaining),
        "attempts": attempts_log,
        "last_error": last_error,
    }


async def place_entry_order(
    token_id: str,
    contracts: float,
    price: float,
    side: SideName,
    *,
    order_mode: OrderModeName = "limit",
    entry_slippage_pct: float = 2.0,
) -> dict[str, Any]:
    """
    כניסה (BUY בדרך כלל). dispatch לפי order_mode:
    - "limit": place_limit_order (GTC). התנהגות קודמת — לא בוצע שינוי.
    - "market": MarketOrderArgs FOK עם amount=contracts*price (דולרים) +
      slippage cap = price * (1 + entry_slippage_pct%). FOK מבטיח שלא תהיה חצי-כניסה.
    """
    if order_mode == "market":
        amount_usd = float(contracts) * float(price)
        slip_price = _clamp_slippage_price(price, side, entry_slippage_pct)
        r = await place_market_order(
            token_id,
            amount_usd,
            side,
            order_type="FOK",
            slippage_cap_price=slip_price,
        )
        # יישור סכימת התגובה לפורמט של place_limit_order (price+size)
        if r.get("ok"):
            r["price"] = float(price)
            r["size"] = float(contracts)
        return r
    return await place_limit_order(token_id, float(price), float(contracts), side)


async def place_exit_order(
    token_id: str,
    contracts: float,
    bid: float,
    *,
    order_mode: OrderModeName = "limit",
    exit_slippage_pct: float = 5.0,
    retry_max_attempts: int = 3,
) -> dict[str, Any]:
    """
    יציאה (SELL). dispatch לפי order_mode:
    - "limit": place_limit_order ב-bid. התנהגות קודמת.
    - "market": FAK אגרסיבי עם slippage רחב + retry ladder אם התמלא חלקית.
      מחזיר price+size מותאמים לחתימה הקודמת כדי שה-caller ימשיך לרשום record_live_sell.
    """
    if order_mode == "market":
        slip_price = _clamp_slippage_price(bid, "SELL", exit_slippage_pct)
        # נסיון ראשון FAK
        r = await place_market_order(
            token_id,
            float(contracts),
            "SELL",
            order_type="FAK",
            slippage_cap_price=slip_price,
        )
        matched_n = 0.0
        if r.get("ok"):
            try:
                matched_n = float(r.get("matched") or 0.0)
            except (TypeError, ValueError):
                matched_n = 0.0

        if r.get("ok") and matched_n < float(contracts) - 1e-6:
            # חלקי — retry ladder
            remaining = float(contracts) - matched_n
            ladder = await _retry_market_sell_ladder(
                token_id,
                remaining,
                bid=float(bid),
                base_slippage_pct=exit_slippage_pct,
                max_attempts=max(0, int(retry_max_attempts) - 1),
            )
            matched_n += float(ladder.get("sold_total_contracts") or 0.0)
            r["ladder"] = ladder

        if r.get("ok"):
            r["price"] = float(bid)  # לצורך record_live_sell — המחיר החוקי שקיבלנו היה >= bid*(1-slip)
            r["size"] = float(matched_n if matched_n > 0 else contracts)
        return r

    return await place_limit_order(token_id, float(bid), float(contracts), "SELL")


def _clob_balance_hint(
    balance_usd: Optional[float],
    *,
    positions_count: int = 0,
) -> Optional[str]:
    """רמזי תצורה/הפקדה כשיתרת CLOB 0 או לא זמינה (מותאם ל-signature_type ב-env)."""
    sig_type = 0
    try:
        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0").strip() or "0")
    except ValueError:
        pass
    if balance_usd is None or balance_usd == 0:
        has_positions = positions_count > 0
        if has_positions:
            return (
                "יתרה $0 אבל יש פוזיציות פתוחות — ייתכן שכל הכסף מושקע. "
                "לכניסות חדשות נדרשת יתרה פנויה ב-CLOB."
            )
        if sig_type == 0:
            return (
                "יתרה $0 ללא פוזיציות. אם ייצאת מפתח מארנק הדפדפן של Polymarket, "
                "הגדר POLYMARKET_SIGNATURE_TYPE=1 (או 2) "
                "ו-POLYMARKET_FUNDER=<כתובת proxy>. ראה docs.polymarket.com — CLOB quickstart."
            )
        return (
            "יתרה $0 ב-CLOB. ודאו שהפקדתם USDC לחשבון המסחר ב-Polymarket "
            "(Deposit דרך polymarket.com). USDC בארנק Polygon לבד לא מספיק — "
            "נדרשת הפקדה לחוזה ה-Exchange."
        )
    return None


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

    # funder — הכתובת שמחזיקה את ה-collateral בפועל (proxy wallet).
    # עבור EOA (signature_type=0) זהה בדרך כלל ל-addr; עבור proxy — שונה.
    funder_addr = None
    try:
        funder_addr = getattr(client, "funder", None)
        if funder_addr is None:
            funder_addr = getattr(getattr(client, "builder", None), "funder", None)
    except Exception:
        pass

    is_proxy = bool(
        funder_addr and addr and funder_addr.lower() != addr.lower()
    )

    return {
        "ok": True,
        "balance_usd": bal_usd,
        "allowance_usd": allow_usd,
        "address": addr,
        "funder_address": funder_addr,
        "is_proxy": is_proxy,
        "hint": _clob_balance_hint(bal_usd, positions_count=0),
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
            "funder_address": acct.get("funder_address"),
            "is_proxy": acct.get("is_proxy", False),
            "balance_usd": None,
            "allowance_usd": None,
            "positions": [],
            "equity_usd": None,
            "hint": None,
            "ts": now,
        }
        _PORTFOLIO_CACHE["ts"] = now
        _PORTFOLIO_CACHE["payload"] = payload
        return payload

    signer_address = acct.get("address") or ""
    funder_address = acct.get("funder_address") or ""
    is_proxy = acct.get("is_proxy", False)
    # פוזיציות מאוחסנות תחת הכתובת שמחזיקה את הכספים — funder עבור proxy, signer עבור EOA.
    positions_address = funder_address if funder_address else signer_address
    positions = await fetch_live_positions(positions_address)
    balance_usd = acct.get("balance_usd")
    total_pos_value = 0.0
    for p in positions:
        v = p.get("value_usd")
        if isinstance(v, (int, float)):
            total_pos_value += float(v)
    equity_usd: Optional[float] = None
    if isinstance(balance_usd, (int, float)):
        equity_usd = float(balance_usd) + total_pos_value

    hint = _clob_balance_hint(balance_usd, positions_count=len(positions))

    payload = {
        "ok": True,
        "address": signer_address or None,
        "funder_address": funder_address or None,
        "is_proxy": is_proxy,
        "balance_usd": balance_usd,
        "allowance_usd": acct.get("allowance_usd"),
        "positions": positions,
        "equity_usd": equity_usd,
        "hint": hint,
        "ts": now,
    }
    _PORTFOLIO_CACHE["ts"] = now
    _PORTFOLIO_CACHE["payload"] = payload
    return payload


def reset_portfolio_cache() -> None:
    _PORTFOLIO_CACHE["ts"] = 0.0
    _PORTFOLIO_CACHE["payload"] = None
