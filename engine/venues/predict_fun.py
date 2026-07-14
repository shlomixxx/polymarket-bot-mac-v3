# engine/venues/predict_fun.py
"""PredictFunVenue — Predict.fun (BNB Chain) venue. M2a = READ-ONLY (discover + book).
M2b-Step-2 = the REAL order path: JWT auth, fetch_portfolio, place_entry_order/place_exit_order
(EIP-712 build+sign via `predict-sdk` -> REST POST /v1/orders). Behind the existing triple lock
(`predict_secrets`): missing wallet key -> refuse; mainnet without full live-enable -> refuse;
testnet + a wallet key -> proceeds and actually submits a (fake-money) order.

Auth recipe (proven live against api-testnet.predict.fun, see
scratchpad/m2b2-build-notes.md and docs/.../m2-research-predict-api.md §1):
  GET /v1/auth/message?address=<addr> -> {"data":{"message": "..."}}  (opaque, timestamp-bound)
  sign EIP-191 personal_sign (`encode_defunct(text=message)`) with the EOA from PREDICT_WALLET_KEY
  POST /v1/auth {"signer":address,"signature":"0x...","message":message} -> {"data":{"token":JWT}}
`eth_account`'s `.hex()` on a signature/key omits the `0x` prefix in this pinned version — the API
requires it, so `_get_jwt()` prefixes it explicitly. The JWT is valid ~24h; re-auth on 401.

Order build/sign (via `predict-sdk`, see the same research note §2 + the installed package's own
README at predict_sdk-0.0.20.dist-info/METADATA): `OrderBuilder.make(chain_id, private_key)` ->
`get_limit_order_amounts`/`get_market_order_amounts` -> `build_order("LIMIT"|"MARKET", ...)` ->
`build_typed_data(is_neg_risk=False, is_yield_bearing=False)` (BTC up/down is always plain,
non-neg-risk, non-yield-bearing) -> `sign_typed_data_order`. None of this touches the network
(pure/offline signing) except `OrderBuilder.make`'s lazy `Web3(HTTPProvider(...))`, which itself
makes no request until a chain-touching method is called — we never call one here, so order
build+sign is safe to exercise for real in unit tests (only the REST leg is mocked).

fetch_chain_shares_for_token stays a stub (None) — on-chain approvals/redeem need gas + collateral
and are a later step (M2b-Step-2 explicitly excludes them; see the task's "Do NOT" list).

Testnet base (`https://api-testnet.predict.fun`) needs no API key for reads; respect the documented
240 req/min.

Field shapes below are taken from a LIVE testnet capture (2026-07-13), not just the docs — see
docs/superpowers/sdd/... research notes `m2-research-market-map.md` / `m2-research-predict-api.md`.
Two things the initial design sketch got wrong, fixed here:
  1. `outcomes[].bestBid`/`bestAsk` are NOT bare floats — they are `null` (no liquidity yet) or a
     `{"price": <0..1>, "size": <shares>}` object. See `_price_of()`.
  2. There is no `minOrderSize` field on the market object (checked the live sample) — Predict's
     docs quote a flat 1 USDT minimum, so we hardcode that instead of reading a phantom key.

Also: `GET /v1/markets/{id}/orderbook` is keyed by the market's numeric `id`, NOT by the outcome's
`onChainId` — and the returned book is priced for the "Yes" (Up) side only ("YES asks == NO bids,
YES bids == NO asks" per docs). `get_book()` looks up which market/outcome a token_id belongs to
and mirrors the Down side at (1 - price) when needed. The inlined `bestBid`/`bestAsk` on each
outcome, by contrast, are already given per-outcome (already mirrored by the API) — no extra work
needed there.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from predict_sdk import (
    ADDRESSES_BY_CHAIN_ID,
    ERC20_ABI,
    RPC_URLS_BY_CHAIN_ID,
    BuildOrderInput,
    Book,
    ChainId,
    InvalidQuantityError,
    LimitHelperInput,
    MarketHelperInput,
    OrderBuilder,
    PredictSDKError,
    Side,
)
from web3 import Web3

import predict_secrets

from .base import ActiveMarket, Venue

_TESTNET_BASE = "https://api-testnet.predict.fun"
_MAINNET_BASE = "https://api.predict.fun"  # wired for real in M3/M4; unused while is_testnet=True

# Best-effort error-code classification for POST /v1/orders failures. Predict.fun's exact error
# payload schema for order rejections is NOT fully documented (see m2-research-predict-api.md §8);
# this is a defensive keyword classifier over whatever code/message the API does surface, not a
# verified exhaustive list — revisit once a funded testnet wallet can trigger real rejections.
# Each entry is (error_code, required_substrings_ALL_of, optional_substrings_ANY_of).
_ORDER_ERROR_KEYWORDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("insufficient_onchain_balance", ("balance",), ("insufficient", "not enough")),
    ("min_order_size", ("size",), ("min", "minimum")),
    ("post_order_timeout", (), ("timeout", "timed out")),
)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _pick_float(d: dict, *keys: str) -> Optional[float]:
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


def _jwt_expiry_epoch(token: str, *, default_ttl: float = 23 * 3600.0) -> float:
    """Best-effort JWT `exp` extraction (no external JWT lib): base64-decode the payload segment.
    Any failure (opaque/malformed token) falls back to a conservative ~23h TTL — the JWT is
    documented to be valid 24h; re-auth-on-401 covers the rest regardless."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp:
            return float(exp) - 60.0  # small safety buffer before the real expiry
    except Exception:
        pass
    return time.time() + default_ttl


def _normalize_predict_position(raw: Any) -> Optional[dict]:
    """GET /v1/positions -> our portfolio contract's position shape. Defensive about exact key
    names (documented shape is `{id, market{...}, outcome, amount, valueUsd}` — see
    m2-research-predict-api.md §3 — but avg/mark price aren't documented, so those may end up
    None), mirroring live_clob._normalize_position_record's tolerance for a Polymarket-side drift."""
    if not isinstance(raw, dict):
        return None
    market = raw.get("market") if isinstance(raw.get("market"), dict) else {}
    tok = (
        raw.get("tokenId") or raw.get("token_id") or raw.get("onChainId")
        or raw.get("asset") or market.get("onChainId")
    )
    size = _pick_float(raw, "amount", "size", "quantity", "shares")
    if tok is None or size is None or size <= 0:
        return None
    value = _pick_float(raw, "valueUsd", "value_usd", "currentValue", "value")
    avg = _pick_float(raw, "avgPrice", "avg_price", "entryPrice")
    mark = _pick_float(raw, "markPrice", "currentPrice", "lastPrice")
    if value is None and mark is not None:
        value = size * mark
    outcome = str(raw.get("outcome") or raw.get("outcomeName") or "").strip()
    norm = outcome.lower()
    if norm in ("up", "yes"):
        side = "Up"
    elif norm in ("down", "no"):
        side = "Down"
    else:
        side = outcome or "Up"
    return {
        "token_id": str(tok),
        "side": side,
        "size": float(size),
        "avg_price": float(avg) if avg is not None else None,
        "mark_price": float(mark) if mark is not None else None,
        "value_usd": float(value) if value is not None else None,
    }


def _classify_order_error(payload: Any, status_code: Optional[int]) -> tuple[str, str]:
    """Best-effort (message, error_code) from a non-OK /v1/orders response. See the
    `_ORDER_ERROR_KEYWORDS` docstring above for the "not fully verified" caveat."""
    text_parts: list[str] = []
    code_hint: Any = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code_hint = err.get("code")
            if err.get("message"):
                text_parts.append(str(err["message"]))
        elif isinstance(err, str) and err:
            text_parts.append(err)
        if payload.get("message"):
            text_parts.append(str(payload["message"]))
        if code_hint is None and payload.get("code"):
            code_hint = payload.get("code")
    message = " ".join(p for p in text_parts if p) or (
        f"predict_fun order rejected (HTTP {status_code})" if status_code is not None
        else "predict_fun order rejected (no response)"
    )
    haystack = f"{code_hint or ''} {message}".lower()
    for code, required_all, optional_any in _ORDER_ERROR_KEYWORDS:
        if all(n in haystack for n in required_all) and (
            not optional_any or any(n in haystack for n in optional_any)
        ):
            return message, code
    if status_code == 401:
        return message, "auth_failed"
    if code_hint:
        return message, str(code_hint).lower()
    return message, "order_rejected"


def _is_testnet() -> bool:
    # Default-safe: only an explicit opt-out points at mainnet.
    return os.environ.get("PREDICT_MAINNET", "").strip() != "1"


def _price_of(value: Any) -> Optional[float]:
    """Normalize outcomes[].bestBid/bestAsk: null | {"price":.., "size":..} | (defensively) a bare
    number, all -> float|None. See module docstring point (1)."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("price")
        if value is None:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PredictFunVenue(Venue):
    name = "predict_fun"
    collateral = "USDT"

    def __init__(self) -> None:
        self._testnet = _is_testnet()
        self._jwt: Optional[str] = None
        self._jwt_exp: float = 0.0

    @property
    def is_testnet(self) -> bool:
        return self._testnet

    @property
    def chain_id(self) -> int:
        return 97 if self._testnet else 56

    @property
    def _sdk_chain_id(self) -> ChainId:
        return ChainId(self.chain_id)

    @property
    def _base(self) -> str:
        return _TESTNET_BASE if self._testnet else _MAINNET_BASE

    # --- REST reads (mockable seams: tests patch these directly) ---
    async def _get_open_crypto_updown_markets(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                f"{self._base}/v1/markets",
                params={"marketVariant": "CRYPTO_UP_DOWN", "status": "OPEN"},
            )
            r.raise_for_status()
            payload = r.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return data if isinstance(data, list) else []

    async def _get_orderbook(self, market_id: Any) -> dict:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{self._base}/v1/markets/{market_id}/orderbook")
            r.raise_for_status()
            payload = r.json()
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    async def _find_market_and_outcome(self, token_id: str) -> Optional[tuple[dict, dict]]:
        """Locate the market + outcome object whose onChainId == token_id (by value, not index)."""
        markets = await self._get_open_crypto_updown_markets()
        for mk in markets:
            for o in mk.get("outcomes", []):
                if str(o.get("onChainId")) == str(token_id):
                    return mk, o
        return None

    # --- Auth (JWT) — see the module docstring for the proven-live recipe ---
    def _get_wallet_key(self) -> Optional[str]:
        key = (os.environ.get("PREDICT_WALLET_KEY") or "").strip()
        return key or None

    def _get_wallet_account(self) -> Optional[Any]:
        key = self._get_wallet_key()
        if key is None:
            return None
        return Account.from_key(key)

    async def _get_auth_message(self, address: str) -> Optional[str]:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(f"{self._base}/v1/auth/message", params={"address": address})
            r.raise_for_status()
            payload = r.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return data.get("message") if isinstance(data, dict) else None

    async def _post_auth(self, address: str, signature: str, message: str) -> Optional[str]:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(
                f"{self._base}/v1/auth",
                json={"signer": address, "signature": signature, "message": message},
            )
            r.raise_for_status()
            payload = r.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return data.get("token") if isinstance(data, dict) else None

    async def _get_jwt(self, *, force: bool = False) -> Optional[str]:
        """Cached JWT, re-authenticating when forced/expired. Returns None (never raises) when no
        wallet key is configured — callers gate on `predict_secrets.has_wallet_key()` first, so a
        None here only happens for a genuine race (key removed mid-call) and is handled the same
        way: the caller treats a missing JWT as "can't proceed"."""
        if not force and self._jwt and time.time() < self._jwt_exp:
            return self._jwt
        account = self._get_wallet_account()
        if account is None:
            return None
        message = await self._get_auth_message(account.address)
        if not message:
            return None
        signature = account.sign_message(encode_defunct(text=message)).signature.hex()
        if not signature.startswith("0x"):
            # eth_account's `.hex()` omits the `0x` prefix on this pinned version — Predict.fun's
            # API 401s without it. See the module docstring / scratchpad/m2b2-build-notes.md.
            signature = "0x" + signature
        token = await self._post_auth(account.address, signature, message)
        if not token:
            return None
        self._jwt = token
        self._jwt_exp = _jwt_expiry_epoch(token)
        return token

    async def _authed_get(self, path: str, *, params: Optional[dict] = None) -> Any:
        """GET an authenticated endpoint, re-authing once on a 401. Returns the unwrapped `data`
        payload (or None if a JWT could not be obtained at all)."""
        token = await self._get_jwt()
        if not token:
            return None
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{self._base}{path}", params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code == 401 and attempt == 0:
                token = await self._get_jwt(force=True)
                if not token:
                    return None
                continue
            r.raise_for_status()
            payload = r.json()
            return payload.get("data", payload) if isinstance(payload, dict) else payload
        return None

    async def _get_positions(self) -> list[dict]:
        data = await self._authed_get("/v1/positions")
        return data if isinstance(data, list) else []

    async def _get_usdt_balance(self, address: str) -> float:
        """Raw ERC20 balanceOf via web3 (no OrderBuilder/network-touching SDK method needed for a
        read-only balance check). USDT on BSC (incl. Predict's testnet mock collateral) is
        18-decimal — see m2-research-predict-api.md §0 "Collateral decimals gotcha"."""
        addresses = ADDRESSES_BY_CHAIN_ID[self._sdk_chain_id]
        rpc_url = RPC_URLS_BY_CHAIN_ID[self._sdk_chain_id]
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        contract = w3.eth.contract(address=Web3.to_checksum_address(addresses.USDT), abi=ERC20_ABI)
        raw = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
        return float(raw) / 1e18

    # --- Venue interface: discovery + book ---
    async def discover_active_window(self, window: str) -> Optional[ActiveMarket]:
        want_sec = 900 if window == "15m" else 300
        prefix = "btc-updown-15m-" if window == "15m" else "btc-updown-5m-"
        markets = await self._get_open_crypto_updown_markets()

        candidates: list[tuple[int, dict]] = []
        for mk in markets:
            slug = str(mk.get("categorySlug", ""))
            if not slug.startswith(prefix):
                continue
            try:
                epoch = int(slug.rsplit("-", 1)[-1])
            except ValueError:
                continue
            candidates.append((epoch, mk))
        if not candidates:
            return None

        # Prefer the window that's live right now; else the nearest upcoming one; else whatever
        # the API gave us (keeps this robust for a mocked single-market response in tests).
        now = int(time.time())
        live = [c for c in candidates if c[0] <= now < c[0] + want_sec]
        if live:
            epoch, mk = live[0]
        else:
            upcoming = [c for c in candidates if c[0] > now]
            epoch, mk = min(upcoming, key=lambda c: c[0]) if upcoming else candidates[0]

        outs = {str(o.get("name", "")).lower(): o for o in mk.get("outcomes", [])}
        up, down = outs.get("up"), outs.get("down")
        if not up or not down:
            return None  # never guess a side from array position

        up_price = _price_of(up.get("bestAsk"))
        down_price = _price_of(down.get("bestAsk"))
        return ActiveMarket(
            slug=str(mk.get("categorySlug", "")),
            epoch=epoch,
            condition_id=str(mk.get("conditionId", "")),
            end_date_iso=str(mk.get("boostEndsAt") or mk.get("endDate") or ""),
            closed=(str(mk.get("tradingStatus", "OPEN")).upper() != "OPEN"),
            token_up=str(up.get("onChainId")),
            token_down=str(down.get("onChainId")),
            outcome_prices=(
                up_price if up_price is not None else 0.0,
                down_price if down_price is not None else 0.0,
            ),
            order_min_size=1.0,  # flat per docs; no per-market min-size key on the live testnet object
            title=str(mk.get("question") or mk.get("title") or ""),
            window_sec=want_sec,
            order_min_size_source="gamma",  # not fetched from an authoritative book endpoint
            resolution_source="predict.fun CRYPTO_UP_DOWN (ChainlinkUpDownAdapter / Pyth)",
        )

    async def best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        # bestBid/bestAsk are already inlined PER OUTCOME on the market object (already mirrored
        # for Down by the API) — no separate book call needed for top-of-book.
        found = await self._find_market_and_outcome(token_id)
        if found is None:
            return (None, None)
        _, outcome = found
        return _price_of(outcome.get("bestBid")), _price_of(outcome.get("bestAsk"))

    async def get_book(self, client: Any, token_id: str) -> dict:
        found = await self._find_market_and_outcome(token_id)
        if found is None:
            return {"bids": [], "asks": []}
        mk, outcome = found
        book = await self._get_orderbook(mk.get("id"))
        raw_bids = book.get("bids") or []
        raw_asks = book.get("asks") or []
        if str(outcome.get("name", "")).lower() == "down":
            # Full-depth book is Yes(Up)-priced only; Down = mirror at (1 - price).
            bids = [(round(1.0 - float(p), 6), q) for p, q in raw_asks]
            asks = [(round(1.0 - float(p), 6), q) for p, q in raw_bids]
        else:
            bids = [(float(p), q) for p, q in raw_bids]
            asks = [(float(p), q) for p, q in raw_asks]
        return {
            "bids": [{"price": p, "size": float(q)} for p, q in bids],
            "asks": [{"price": p, "size": float(q)} for p, q in asks],
        }

    # --- orders: the M2b real path, gated by the triple lock ---
    def _gate(self) -> Optional[dict]:
        """Fail-closed pre-flight, checked before anything else touches the network or the SDK.
        Testnet + a wallet key proceeds (submits a real, but fake-money, order). Mainnet needs the
        full `predict_secrets.is_live_enabled()` triple lock, not just a wallet key."""
        if not predict_secrets.has_wallet_key():
            return {
                "ok": False,
                "error": "Predict.fun wallet key not configured (PREDICT_WALLET_KEY)",
                "error_code": "no_wallet_key",
            }
        if not predict_secrets.is_testnet() and not predict_secrets.is_live_enabled():
            return {
                "ok": False,
                "error": predict_secrets.live_disabled_reason(),
                "error_code": "live_disabled",
            }
        return None

    async def place_entry_order(self, token_id: str, contracts: float, price: float, side: str,
                                 *, order_mode: str = "limit", entry_slippage_pct: float = 2.0) -> dict:
        gate_err = self._gate()
        if gate_err:
            return gate_err
        sdk_side = Side.SELL if str(side).upper() == "SELL" else Side.BUY
        return await self._submit_order(
            token_id=token_id, side=sdk_side, order_mode=order_mode,
            price=price, contracts=contracts, slippage_pct=entry_slippage_pct,
        )

    async def place_exit_order(self, token_id: str, contracts: float, bid: float,
                                *, order_mode: str = "limit", exit_slippage_pct: float = 5.0,
                                retry_max_attempts: int = 3) -> dict:
        gate_err = self._gate()
        if gate_err:
            return gate_err
        return await self._submit_order(
            token_id=token_id, side=Side.SELL, order_mode=order_mode,
            price=bid, contracts=contracts, slippage_pct=exit_slippage_pct,
        )

    async def _submit_order(self, *, token_id: str, side: Any, order_mode: str, price: float,
                             contracts: float, slippage_pct: float) -> dict:
        key = self._get_wallet_key()
        if key is None:
            # Defensive only — _gate() already checked has_wallet_key() before this was called.
            return {
                "ok": False,
                "error": "Predict.fun wallet key not configured (PREDICT_WALLET_KEY)",
                "error_code": "no_wallet_key",
            }
        found = await self._find_market_and_outcome(token_id)
        if found is None:
            return {
                "ok": False,
                "error": f"predict_fun: unknown token_id {token_id!r} (not an open market)",
                "error_code": "unknown_market",
            }
        market, _outcome = found
        fee_rate_bps = int(market.get("feeRateBps") or 0)
        is_neg_risk = bool(market.get("isNegRisk", False))
        is_yield_bearing = bool(market.get("isYieldBearing", False))

        builder = OrderBuilder.make(self._sdk_chain_id, key)
        quantity_wei = int(round(float(contracts) * 10**18))
        strategy = "MARKET" if order_mode == "market" else "LIMIT"
        slippage_bps = max(0, int(round(float(slippage_pct) * 100)))

        try:
            if strategy == "LIMIT":
                price_wei = int(round(float(price) * 10**18))
                amounts = builder.get_limit_order_amounts(
                    LimitHelperInput(
                        side=side, price_per_share_wei=price_wei, quantity_wei=quantity_wei,
                    )
                )
            else:
                book_dict = await self.get_book(None, token_id)
                book = Book(
                    market_id=int(market.get("id") or 0),
                    update_timestamp_ms=int(time.time() * 1000),
                    asks=[(float(a["price"]), float(a["size"])) for a in book_dict.get("asks", [])],
                    bids=[(float(b["price"]), float(b["size"])) for b in book_dict.get("bids", [])],
                )
                is_min_amount_out = side == Side.BUY and slippage_bps > 0
                amounts = builder.get_market_order_amounts(
                    MarketHelperInput(
                        side=side, quantity_wei=quantity_wei, slippage_bps=slippage_bps,
                        is_min_amount_out=is_min_amount_out,
                    ),
                    book,
                )

            order = builder.build_order(strategy, BuildOrderInput(
                side=side, token_id=str(token_id), maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount), fee_rate_bps=fee_rate_bps,
            ))
            typed_data = builder.build_typed_data(
                order, is_neg_risk=is_neg_risk, is_yield_bearing=is_yield_bearing,
            )
            signed = builder.sign_typed_data_order(typed_data)
            order_hash = builder.build_typed_data_hash(typed_data)
            order_signature = signed.signature
            if not order_signature.startswith("0x"):
                # The SDK's own `sign_typed_data_order` hits the SAME unprefixed-`.hex()` quirk as
                # the auth handshake (this pinned eth_account version omits `0x`) — apply the same
                # defensive fix rather than trust the SDK's output is always prefixed.
                order_signature = "0x" + order_signature
        except InvalidQuantityError as e:
            # The SDK enforces its own minimum (quantity_wei >= 1e16, i.e. 0.01 shares) client-side,
            # before any network round trip — treat it the same as the API's own min-size rejection.
            return {
                "ok": False, "error": f"predict_fun: {e}", "error_code": "min_order_size",
            }
        except PredictSDKError as e:
            return {
                "ok": False, "error": f"predict_fun: order build/sign failed: {e}",
                "error_code": "sign_failed",
            }

        body = {
            "data": {
                "order": {
                    "salt": signed.salt, "maker": signed.maker, "signer": signed.signer,
                    "taker": signed.taker, "tokenId": signed.token_id,
                    "makerAmount": signed.maker_amount, "takerAmount": signed.taker_amount,
                    "expiration": signed.expiration, "nonce": signed.nonce,
                    "feeRateBps": signed.fee_rate_bps, "side": int(signed.side),
                    "signatureType": int(signed.signature_type),
                    "signature": order_signature, "hash": order_hash,
                },
                "strategy": strategy,
                "pricePerShare": str(amounts.price_per_share),
                "slippageBps": str(amounts.slippage_bps),
                "isMinAmountOut": amounts.is_min_amount_out,
                "isFillOrKill": False,
            },
        }
        try:
            resp = await self._post_order(body)
        except Exception as e:
            return {
                "ok": False, "error": f"predict_fun: order POST failed: {e}",
                "error_code": "request_failed",
            }
        return self._map_order_result(resp, amounts)

    async def _post_order(self, body: dict) -> dict:
        token = await self._get_jwt()
        if not token:
            raise RuntimeError("predict_fun: could not authenticate (no JWT)")
        status: Optional[int] = None
        payload: Any = None
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    f"{self._base}/v1/orders", json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            status = r.status_code
            payload = _safe_json(r)
            if status == 401 and attempt == 0:
                token = await self._get_jwt(force=True)
                if not token:
                    break
                continue
            break
        return {"status_code": status, "payload": payload}

    def _map_order_result(self, resp: dict, amounts: Any) -> dict:
        status = resp.get("status_code")
        payload = resp.get("payload")
        ok = (
            isinstance(status, int) and 200 <= status < 300
            and (not isinstance(payload, dict) or payload.get("success", True))
        )
        if ok:
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            order_id = data.get("orderHash") if isinstance(data, dict) else None
            price_per_share = (
                amounts.price_per_share / 1e18 if amounts.price_per_share else None
            )
            size = amounts.amount / 1e18 if amounts.amount else 0.0
            return {
                "ok": True, "order_id": order_id, "fill_price": None,
                "price": price_per_share, "size": size, "matched": False,
            }
        message, code = _classify_order_error(payload, status)
        return {"ok": False, "error": message, "error_code": code}

    async def fetch_portfolio(self, *, force: bool = False) -> dict:
        if not predict_secrets.has_wallet_key():
            return {
                "ok": False, "error": "predict_fun portfolio needs a wallet key (PREDICT_WALLET_KEY)",
                "balance_usd": 0.0, "positions": [], "equity_usd": 0.0, "address": None,
                "funder_address": None, "is_proxy": False,
                "hint": "add a wallet key (PREDICT_WALLET_KEY) to enable portfolio reads",
            }
        account = self._get_wallet_account()
        if account is None:
            return {
                "ok": False, "error": "predict_fun portfolio needs a wallet key (PREDICT_WALLET_KEY)",
                "balance_usd": 0.0, "positions": [], "equity_usd": 0.0, "address": None,
                "funder_address": None, "is_proxy": False,
                "hint": "add a wallet key (PREDICT_WALLET_KEY) to enable portfolio reads",
            }
        address = account.address
        try:
            balance_usd = await self._get_usdt_balance(address)
            raw_positions = await self._get_positions()
        except Exception as e:
            return {
                "ok": False, "error": str(e), "balance_usd": None, "positions": [],
                "equity_usd": None, "address": address, "funder_address": address,
                "is_proxy": False, "hint": None,
            }
        positions = [p for p in (_normalize_predict_position(r) for r in raw_positions) if p]
        total_value = sum(
            p["value_usd"] for p in positions if isinstance(p.get("value_usd"), (int, float))
        )
        equity_usd = balance_usd + total_value if isinstance(balance_usd, (int, float)) else None
        return {
            "ok": True, "balance_usd": balance_usd, "positions": positions,
            "equity_usd": equity_usd, "address": address, "funder_address": address,
            "is_proxy": False, "hint": None,
        }

    async def fetch_chain_shares_for_token(self, token_id: str) -> Optional[float]:
        # On-chain approvals/redeem need gas + collateral — a later step (see module docstring).
        return None

    def fetch_account(self) -> dict:
        return {"ok": False, "error": "predict_fun account is M2b"}

    def reset_caches(self) -> None:
        self._jwt = None
        self._jwt_exp = 0.0
        return None

    def live_disabled_reason(self) -> Optional[str]:
        # M2b: delegate to the dedicated triple-lock helper (PREDICT_LIVE + wallet key +
        # not-testnet) instead of an inline, partial check — single source of truth also
        # used by strategy_runner._live_trading_ok().
        return predict_secrets.live_disabled_reason()
