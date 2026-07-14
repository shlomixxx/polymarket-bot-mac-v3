# engine/tests/test_predict_fun_order_path.py
"""M2b-Step-2 — PredictFunVenue's REAL order path: JWT auth, gating, EIP-712 order build+sign via
`predict-sdk`, POST /v1/orders, fetch_portfolio. TDD, NO network: every httpx.AsyncClient the venue
constructs is redirected (via `httpx.MockTransport`) to an in-process handler, so the REAL request
code (method, path, headers, JSON body, retry-on-401) runs — only the wire is faked. The SDK's
order build/sign is NOT mocked: it is pure/offline crypto (see predict_fun.py's module docstring),
so letting it run for real is both safer (it actually exercises correctness of our glue) and
sufficient to keep this file network-free. web3 is faked with a tiny stand-in Contract/Web3 pair.

NOTE ON THE ONE OPEN QUESTION: Predict.fun's exact error-payload schema for a rejected order isn't
documented (m2-research-predict-api.md §8) — `_classify_order_error`'s mapping is a best-effort
keyword classifier, tested here against *plausible* shapes, not a verified-live one.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional
from unittest.mock import AsyncMock

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import predict_secrets
from venues.predict_fun import PredictFunVenue
import venues.predict_fun as pf_module

# A deterministic throwaway key — NOT a funded wallet, purely for offline signature verification.
_TEST_KEY = "0x" + "11" * 32
_TEST_ADDR = Account.from_key(_TEST_KEY).address

_AUTH_MESSAGE = "Please sign this message to log in. Timestamp: 1784000000000"

# Mirrors test_predict_fun_venue.py's _MARKET fixture, plus the isNegRisk/isYieldBearing flags the
# order path reads (BTC up/down is always plain — see m2-research-predict-api.md §6).
_MARKET = {
    "id": 778011, "conditionId": "0x40c806", "categorySlug": "btc-updown-5m-1700000100",
    "status": "OPEN", "tradingStatus": "OPEN", "feeRateBps": 200,
    "isNegRisk": False, "isYieldBearing": False,
    "outcomes": [
        {"indexSet": 2, "name": "Down", "onChainId": "24946845",
         "bestBid": {"price": 0.12, "size": 445470.0}, "bestAsk": {"price": 0.13, "size": 391793.66}},
        {"indexSet": 1, "name": "Up", "onChainId": "45899948",
         "bestBid": {"price": 0.87, "size": 391793.66}, "bestAsk": {"price": 0.88, "size": 445470.0}},
    ],
}
_UP_TOKEN = "45899948"


# `pf_module.httpx` IS the same module object as this file's `httpx` import (modules are
# singletons) — so `_mock_client_factory` must close over the REAL class captured before any
# monkeypatching, or calling it would recurse into the patched name forever.
_RealAsyncClient = httpx.AsyncClient


def _mock_client_factory(handler: Callable[[httpx.Request], httpx.Response]):
    """Returns a drop-in replacement for `httpx.AsyncClient` bound to a MockTransport, so
    `venues.predict_fun`'s real `async with httpx.AsyncClient(...) as c:` code runs unmodified."""
    def _factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=httpx.MockTransport(handler))
    return _factory


def _install_client(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]):
    monkeypatch.setattr(pf_module.httpx, "AsyncClient", _mock_client_factory(handler))


def _auth_handler(calls: list, *, token: str = "jwt-token-1", message: str = _AUTH_MESSAGE):
    """A handler that serves GET /v1/auth/message + POST /v1/auth, recording every request seen."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/v1/auth/message"):
            return httpx.Response(200, json={"success": True, "data": {"message": message}})
        if request.method == "POST" and path.endswith("/v1/auth"):
            return httpx.Response(200, json={"success": True, "data": {"token": token}})
        raise AssertionError(f"unexpected request in auth handler: {request.method} {request.url}")
    return handler


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("PREDICT_LIVE", "PREDICT_WALLET_KEY", "PREDICT_TESTNET"):
        monkeypatch.delenv(v, raising=False)


# --------------------------------------------------------------------------------------------
# Auth handshake
# --------------------------------------------------------------------------------------------

class TestAuthHandshake:
    @pytest.mark.asyncio
    async def test_signs_with_0x_prefix_and_posts_signer_field_not_address(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        calls: list = []
        _install_client(monkeypatch, _auth_handler(calls))

        v = PredictFunVenue()
        token = await v._get_jwt()

        assert token == "jwt-token-1"
        assert len(calls) == 2
        get_req, post_req = calls
        assert get_req.method == "GET" and get_req.url.path.endswith("/v1/auth/message")
        assert get_req.url.params.get("address") == _TEST_ADDR

        assert post_req.method == "POST" and post_req.url.path.endswith("/v1/auth")
        body = json.loads(post_req.content)
        assert set(body.keys()) == {"signer", "signature", "message"}
        assert body["signer"] == _TEST_ADDR
        assert body["message"] == _AUTH_MESSAGE
        assert body["signature"].startswith("0x")
        # And the signature must actually verify against the message + address (real EIP-191
        # personal_sign, not a stub) — this is the whole point of NOT mocking eth_account here.
        recovered = Account.recover_message(encode_defunct(text=_AUTH_MESSAGE), signature=body["signature"])
        assert recovered == _TEST_ADDR

    @pytest.mark.asyncio
    async def test_no_wallet_key_returns_none_and_touches_no_network(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make any HTTP request without a wallet key")
        _install_client(monkeypatch, handler)

        v = PredictFunVenue()
        assert await v._get_jwt() is None

    @pytest.mark.asyncio
    async def test_jwt_is_cached_then_reused_without_a_second_network_round_trip(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        calls: list = []
        _install_client(monkeypatch, _auth_handler(calls))

        v = PredictFunVenue()
        token1 = await v._get_jwt()
        token2 = await v._get_jwt()  # should be served from cache, not re-fetched

        assert token1 == token2 == "jwt-token-1"
        assert len(calls) == 2  # only ONE message+token round trip total

    @pytest.mark.asyncio
    async def test_force_refresh_re_authenticates(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        calls: list = []
        _install_client(monkeypatch, _auth_handler(calls, token="jwt-token-2"))

        v = PredictFunVenue()
        await v._get_jwt()
        await v._get_jwt(force=True)

        assert len(calls) == 4  # two full handshakes

    @pytest.mark.asyncio
    async def test_reset_caches_clears_the_jwt(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        calls: list = []
        _install_client(monkeypatch, _auth_handler(calls))

        v = PredictFunVenue()
        await v._get_jwt()
        v.reset_caches()
        await v._get_jwt()

        assert len(calls) == 4  # cache was cleared, so a second full handshake happened


# --------------------------------------------------------------------------------------------
# Gating truth table (fail-closed; must never touch the network when a lock is closed)
# --------------------------------------------------------------------------------------------

class TestGating:
    def _deny_all_network(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"gated call must not hit the network: {request.url}")
        _install_client(monkeypatch, handler)

    @pytest.mark.asyncio
    async def test_no_wallet_key_refuses_with_no_wallet_key_code(self, monkeypatch):
        self._deny_all_network(monkeypatch)
        v = PredictFunVenue()
        r = await v.place_entry_order(_UP_TOKEN, 10.0, 0.5, "BUY")
        assert r == {
            "ok": False,
            "error": "Predict.fun wallet key not configured (PREDICT_WALLET_KEY)",
            "error_code": "no_wallet_key",
        }
        r2 = await v.place_exit_order(_UP_TOKEN, 10.0, 0.5)
        assert r2["ok"] is False and r2["error_code"] == "no_wallet_key"

    @pytest.mark.asyncio
    async def test_mainnet_without_live_enable_refuses_with_live_disabled_code(self, monkeypatch):
        self._deny_all_network(monkeypatch)
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        monkeypatch.setenv("PREDICT_TESTNET", "0")  # -> predict_secrets.is_testnet() == False
        # PREDICT_LIVE left unset -> is_live_enabled() == False
        v = PredictFunVenue()
        r = await v.place_entry_order(_UP_TOKEN, 10.0, 0.5, "BUY")
        assert r["ok"] is False
        assert r["error_code"] == "live_disabled"
        assert r["error"] == predict_secrets.live_disabled_reason()

    @pytest.mark.asyncio
    async def test_mainnet_fully_live_enabled_passes_the_gate(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        monkeypatch.setenv("PREDICT_TESTNET", "0")
        monkeypatch.setenv("PREDICT_LIVE", "1")
        assert predict_secrets.is_live_enabled() is True
        v = PredictFunVenue()
        assert v._gate() is None

    @pytest.mark.asyncio
    async def test_testnet_with_wallet_key_passes_the_gate_even_without_live_flag(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        # PREDICT_TESTNET unset -> default-safe testnet; PREDICT_LIVE unset.
        v = PredictFunVenue()
        assert v._gate() is None


# --------------------------------------------------------------------------------------------
# Order build/sign/POST (the SDK runs for real; only the REST leg + market lookup are faked)
# --------------------------------------------------------------------------------------------

def _order_post_handler(calls: list, *, order_hash: str = "0xabc123",
                         auth_calls: Optional[list] = None):
    auth_calls = auth_calls if auth_calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/v1/auth/message"):
            auth_calls.append(request)
            return httpx.Response(200, json={"success": True, "data": {"message": _AUTH_MESSAGE}})
        if request.method == "POST" and path.endswith("/v1/auth"):
            auth_calls.append(request)
            return httpx.Response(200, json={"success": True, "data": {"token": "jwt-orders"}})
        if request.method == "POST" and path.endswith("/v1/orders"):
            calls.append(request)
            return httpx.Response(
                200, json={"success": True, "data": {"code": "OK", "orderHash": order_hash}}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")
    return handler


@pytest.fixture
def _testnet_wallet(monkeypatch):
    monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)


class TestOrderBuildSignPost:
    @pytest.mark.asyncio
    async def test_limit_buy_builds_signs_and_posts_the_expected_body(self, monkeypatch, _testnet_wallet):
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome",
                             AsyncMock(return_value=(_MARKET, _MARKET["outcomes"][1])))
        order_calls: list = []
        _install_client(monkeypatch, _order_post_handler(order_calls))

        result = await v.place_entry_order(_UP_TOKEN, 10.0, 0.52, "BUY",
                                            order_mode="limit", entry_slippage_pct=2.0)

        assert result["ok"] is True
        assert result["order_id"] == "0xabc123"
        assert result["price"] == pytest.approx(0.52, abs=1e-6)
        assert result["size"] == pytest.approx(10.0, abs=1e-6)
        assert result["matched"] is False

        assert len(order_calls) == 1
        body = json.loads(order_calls[0].content)
        order = body["data"]["order"]
        assert order["maker"] == order["signer"] == _TEST_ADDR
        assert order["taker"] == "0x" + "0" * 40  # ZERO_ADDRESS: a public order
        assert order["tokenId"] == _UP_TOKEN
        assert order["feeRateBps"] == "200"
        assert order["side"] == 0  # Side.BUY
        assert order["signatureType"] == 0  # SignatureType.EOA
        assert order["signature"].startswith("0x")
        assert order["salt"] and str(order["salt"]).isdigit()
        assert int(order["expiration"]) > time.time()
        assert body["data"]["strategy"] == "LIMIT"
        assert body["data"]["isFillOrKill"] is False
        assert float(body["data"]["pricePerShare"]) == pytest.approx(0.52 * 1e18, rel=1e-9)

    @pytest.mark.asyncio
    async def test_market_sell_uses_the_book_and_posts_market_strategy(self, monkeypatch, _testnet_wallet):
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome",
                             AsyncMock(return_value=(_MARKET, _MARKET["outcomes"][1])))
        monkeypatch.setattr(v, "get_book", AsyncMock(return_value={
            "bids": [{"price": 0.85, "size": 500.0}],
            "asks": [{"price": 0.88, "size": 500.0}],
        }))
        order_calls: list = []
        _install_client(monkeypatch, _order_post_handler(order_calls))

        result = await v.place_exit_order(_UP_TOKEN, 5.0, 0.85, order_mode="market",
                                           exit_slippage_pct=5.0)

        assert result["ok"] is True
        body = json.loads(order_calls[0].content)
        assert body["data"]["strategy"] == "MARKET"
        assert body["data"]["order"]["side"] == 1  # Side.SELL

    @pytest.mark.asyncio
    async def test_unknown_token_id_is_rejected_before_any_signing_or_network(self, monkeypatch, _testnet_wallet):
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome", AsyncMock(return_value=None))

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not hit the network for an unknown market")
        _install_client(monkeypatch, handler)

        result = await v.place_entry_order("does-not-exist", 1.0, 0.5, "BUY")
        assert result["ok"] is False
        assert result["error_code"] == "unknown_market"


# --------------------------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------------------------

class TestErrorMapping:
    def _post_error(self, monkeypatch, *, status: int, payload: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path.endswith("/v1/auth/message"):
                return httpx.Response(200, json={"success": True, "data": {"message": _AUTH_MESSAGE}})
            if request.method == "POST" and path.endswith("/v1/auth"):
                return httpx.Response(200, json={"success": True, "data": {"token": "jwt-err"}})
            if request.method == "POST" and path.endswith("/v1/orders"):
                return httpx.Response(status, json=payload)
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        _install_client(monkeypatch, handler)

    @pytest.mark.asyncio
    async def test_insufficient_balance_is_mapped(self, monkeypatch, _testnet_wallet):
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome",
                             AsyncMock(return_value=(_MARKET, _MARKET["outcomes"][1])))
        self._post_error(monkeypatch, status=400, payload={
            "success": False,
            "error": {"code": "INSUFFICIENT_BALANCE", "message": "insufficient onchain balance"},
        })
        r = await v.place_entry_order(_UP_TOKEN, 10.0, 0.52, "BUY")
        assert r["ok"] is False
        assert r["error_code"] == "insufficient_onchain_balance"

    @pytest.mark.asyncio
    async def test_min_order_size_surfaced_by_the_api_is_mapped(self, monkeypatch, _testnet_wallet):
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome",
                             AsyncMock(return_value=(_MARKET, _MARKET["outcomes"][1])))
        self._post_error(monkeypatch, status=400, payload={
            "success": False,
            "error": {"code": "MIN_SIZE", "message": "below minimum order size"},
        })
        # A normal-sized order so the SDK's own client-side check (see the next test) doesn't
        # short-circuit before the (mocked) API gets a chance to reject it.
        r = await v.place_entry_order(_UP_TOKEN, 10.0, 0.52, "BUY")
        assert r["ok"] is False
        assert r["error_code"] == "min_order_size"

    @pytest.mark.asyncio
    async def test_tiny_quantity_is_rejected_client_side_before_any_network_call(
        self, monkeypatch, _testnet_wallet
    ):
        """The SDK enforces quantity_wei >= 1e16 (0.01 shares) itself — this must be surfaced the
        same way as an API-side min-size rejection, and without ever reaching the network."""
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome",
                             AsyncMock(return_value=(_MARKET, _MARKET["outcomes"][1])))

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not reach the network for a client-side-rejected quantity")
        _install_client(monkeypatch, handler)

        r = await v.place_entry_order(_UP_TOKEN, 0.001, 0.52, "BUY")
        assert r["ok"] is False
        assert r["error_code"] == "min_order_size"

    @pytest.mark.asyncio
    async def test_timeout_is_mapped(self, monkeypatch, _testnet_wallet):
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome",
                             AsyncMock(return_value=(_MARKET, _MARKET["outcomes"][1])))
        self._post_error(monkeypatch, status=408, payload={
            "success": False, "error": {"message": "post order timeout"},
        })
        r = await v.place_entry_order(_UP_TOKEN, 10.0, 0.52, "BUY")
        assert r["ok"] is False
        assert r["error_code"] == "post_order_timeout"

    @pytest.mark.asyncio
    async def test_unrecognized_error_falls_back_to_generic_code(self, monkeypatch, _testnet_wallet):
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_find_market_and_outcome",
                             AsyncMock(return_value=(_MARKET, _MARKET["outcomes"][1])))
        self._post_error(monkeypatch, status=500, payload={
            "success": False, "error": {"message": "something exploded"},
        })
        r = await v.place_entry_order(_UP_TOKEN, 10.0, 0.52, "BUY")
        assert r["ok"] is False
        assert r["error_code"] == "order_rejected"


# --------------------------------------------------------------------------------------------
# fetch_portfolio
# --------------------------------------------------------------------------------------------

class TestFetchPortfolio:
    @pytest.mark.asyncio
    async def test_no_wallet_key_returns_the_benign_placeholder(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not hit the network without a wallet key")
        _install_client(monkeypatch, handler)

        v = PredictFunVenue()
        p = await v.fetch_portfolio()
        assert p["ok"] is False
        assert p["balance_usd"] == 0.0
        assert p["positions"] == []
        assert p["address"] is None

    @pytest.mark.asyncio
    async def test_with_wallet_key_combines_onchain_balance_and_authed_positions(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_get_usdt_balance", AsyncMock(return_value=12.5))
        monkeypatch.setattr(v, "_get_positions", AsyncMock(return_value=[
            {"tokenId": _UP_TOKEN, "outcome": "Up", "amount": "3.0", "valueUsd": "1.5"},
        ]))

        p = await v.fetch_portfolio()

        assert p["ok"] is True
        assert p["balance_usd"] == 12.5
        assert p["address"] == _TEST_ADDR
        assert p["funder_address"] == _TEST_ADDR
        assert p["is_proxy"] is False
        assert p["positions"] == [{
            "token_id": _UP_TOKEN, "side": "Up", "size": 3.0,
            "avg_price": None, "mark_price": None, "value_usd": 1.5,
        }]
        assert p["equity_usd"] == pytest.approx(14.0)

    @pytest.mark.asyncio
    async def test_network_failure_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        v = PredictFunVenue()
        monkeypatch.setattr(v, "_get_usdt_balance", AsyncMock(side_effect=RuntimeError("rpc down")))

        p = await v.fetch_portfolio()
        assert p["ok"] is False
        assert "rpc down" in p["error"]

    @pytest.mark.asyncio
    async def test_get_usdt_balance_builds_the_correct_contract_call(self, monkeypatch):
        """Exercises the REAL _get_usdt_balance body (not just a patched seam): verifies it reads
        the testnet USDT address from the SDK's own ADDRESSES_BY_CHAIN_ID (never hand-rolled) and
        divides by 1e18 (USDT on BSC/testnet is 18-decimal, not 6 — see the module docstring)."""
        seen = {}

        class _FakeFunctions:
            def __init__(self, outer):
                self._outer = outer

            def balanceOf(self, address):  # noqa: N802 (mirrors the real web3 API)
                seen["address"] = address

                class _Call:
                    def call(self_inner):
                        return 5_000_000_000_000_000_000  # 5.0 USDT at 18 decimals

                return _Call()

        class _FakeContract:
            def __init__(self, address, abi):
                seen["contract_address"] = address
                seen["abi"] = abi
                self.functions = _FakeFunctions(self)

        class _FakeEth:
            def contract(self, address, abi):
                return _FakeContract(address, abi)

        class _FakeWeb3:
            def __init__(self, provider):
                seen["rpc_provider"] = provider
                self.eth = _FakeEth()

            HTTPProvider = staticmethod(lambda url: seen.setdefault("rpc_url", url) or url)

            @staticmethod
            def to_checksum_address(addr):
                return addr

        monkeypatch.setattr(pf_module, "Web3", _FakeWeb3)

        v = PredictFunVenue()
        balance = await v._get_usdt_balance(_TEST_ADDR)

        assert balance == pytest.approx(5.0)
        assert seen["contract_address"] == "0xB32171ecD878607FFc4F8FC0bCcE6852BB3149E0"
        assert seen["address"] == _TEST_ADDR
        assert seen["rpc_url"] == "https://bsc-testnet-dataseed.bnbchain.org/"
