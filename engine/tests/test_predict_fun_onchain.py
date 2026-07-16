# engine/tests/test_predict_fun_onchain.py
"""M2b on-chain completion — PredictFunVenue.ensure_approvals()/redeem_positions()/
fetch_chain_shares_for_token(). TDD, NO live network/gas: every chain-touching call goes through
`predict_sdk.OrderBuilder` or `web3.Web3`, both of which are monkeypatched at the module level
(`pf_module.OrderBuilder` / `pf_module.Web3`) with fakes — the SAME seam pattern already used by
test_predict_fun_order_path.py's `_get_usdt_balance` test. This lets the REAL PredictFunVenue code
(gating, arg-building, response-shape mapping) run unmodified while never touching bsc-testnet-dataseed
or spending a drop of tBNB. Live verification (real gas, real approvals/redeem) happens later, once
the testnet wallet is funded — see scripts/predict_testnet_fund_and_prepare.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from eth_account import Account

import predict_secrets
import venues.predict_fun as pf_module
from venues.predict_fun import PredictFunVenue

# A deterministic throwaway key — NOT a funded wallet, purely for offline testing.
_TEST_KEY = "0x" + "22" * 32
_TEST_ADDR = Account.from_key(_TEST_KEY).address

# The real testnet CONDITIONAL_TOKENS address (see predict_sdk.ADDRESSES_BY_CHAIN_ID[BNB_TESTNET])
# — asserted against so a wrong-contract regression (e.g. reading the exchange instead) is caught.
_TESTNET_CONDITIONAL_TOKENS = "0x2827AAef52D71910E8FBad2FfeBC1B6C2DA37743"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("PREDICT_LIVE", "PREDICT_WALLET_KEY", "PREDICT_TESTNET"):
        monkeypatch.delenv(v, raising=False)


def _boom_order_builder(monkeypatch, message: str = "must not touch the SDK when gated"):
    """Install an OrderBuilder stand-in whose .make() raises — proves a gated call never reaches
    the SDK at all (not just that its result is discarded)."""
    class _Boom:
        @staticmethod
        def make(*_a, **_kw):
            raise AssertionError(message)
    monkeypatch.setattr(pf_module, "OrderBuilder", _Boom)


def _boom_web3(monkeypatch, message: str = "must not touch web3 when gated"):
    class _Boom:
        def __init__(self, *_a, **_kw):
            raise AssertionError(message)
    monkeypatch.setattr(pf_module, "Web3", _Boom)


def _fake_order_builder(monkeypatch, builder) -> Mock:
    """Install a fake `OrderBuilder` class whose `.make(chain_id, key)` returns `builder`.
    Returns the fake class so tests can assert on `.make`'s call args."""
    fake_cls = Mock()
    fake_cls.make = Mock(return_value=builder)
    monkeypatch.setattr(pf_module, "OrderBuilder", fake_cls)
    return fake_cls


# ------------------------------------------------------------------------------------------------
# ensure_approvals()
# ------------------------------------------------------------------------------------------------

class TestEnsureApprovals:
    @pytest.mark.asyncio
    async def test_no_wallet_key_refuses_without_touching_the_sdk(self, monkeypatch):
        _boom_order_builder(monkeypatch)
        v = PredictFunVenue()
        result = await v.ensure_approvals()
        assert result == {
            "ok": False, "steps_run": 0,
            "error": "Predict.fun wallet key not configured (PREDICT_WALLET_KEY)",
            "error_code": "no_wallet_key",
        }

    @pytest.mark.asyncio
    async def test_mainnet_without_live_enable_refuses(self, monkeypatch):
        _boom_order_builder(monkeypatch)
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        monkeypatch.setenv("PREDICT_TESTNET", "0")  # -> mainnet; PREDICT_LIVE unset
        v = PredictFunVenue()
        result = await v.ensure_approvals()
        assert result["ok"] is False
        assert result["steps_run"] == 0
        assert result["error_code"] == "live_disabled"
        assert result["error"] == predict_secrets.live_disabled_reason()

    @pytest.mark.asyncio
    async def test_testnet_runs_the_sdk_approval_steps_and_reports_ok(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        fake_steps = ["step-ctf-erc1155", "step-ctf-erc20", "step-negrisk-adapter"]
        report = SimpleNamespace(success=True, steps=[
            SimpleNamespace(status="confirmed"),
            SimpleNamespace(status="skipped"),   # already approved -> not re-sent
            SimpleNamespace(status="confirmed"),
        ])
        builder = Mock()
        builder.get_all_approval_steps = Mock(return_value=fake_steps)
        builder.run_approvals_async = AsyncMock(return_value=report)
        fake_cls = _fake_order_builder(monkeypatch, builder)

        v = PredictFunVenue()
        result = await v.ensure_approvals()

        fake_cls.make.assert_called_once_with(v._sdk_chain_id, _TEST_KEY)
        builder.get_all_approval_steps.assert_called_once_with(is_yield_bearing=False)
        builder.run_approvals_async.assert_awaited_once_with(fake_steps)
        assert result == {"ok": True, "steps_run": 2}  # 2 confirmed, 1 skipped

    @pytest.mark.asyncio
    async def test_testnet_reports_the_failed_step_cause_when_a_step_fails(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        report = SimpleNamespace(success=False, steps=[
            SimpleNamespace(status="confirmed"),
            SimpleNamespace(status="failed",
                             transaction=SimpleNamespace(cause=RuntimeError("insufficient funds for gas"))),
        ])
        builder = Mock()
        builder.get_all_approval_steps = Mock(return_value=["a", "b"])
        builder.run_approvals_async = AsyncMock(return_value=report)
        _fake_order_builder(monkeypatch, builder)

        v = PredictFunVenue()
        result = await v.ensure_approvals()

        assert result["ok"] is False
        assert result["steps_run"] == 2
        assert "insufficient funds for gas" in result["error"]

    @pytest.mark.asyncio
    async def test_sdk_exception_is_caught_and_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        fake_cls = Mock()
        fake_cls.make = Mock(side_effect=RuntimeError("rpc unreachable"))
        monkeypatch.setattr(pf_module, "OrderBuilder", fake_cls)

        v = PredictFunVenue()
        result = await v.ensure_approvals()

        assert result["ok"] is False
        assert result["steps_run"] == 0
        assert "rpc unreachable" in result["error"]


# ------------------------------------------------------------------------------------------------
# redeem_positions()
# ------------------------------------------------------------------------------------------------

class TestRedeemPositions:
    @pytest.mark.asyncio
    async def test_no_wallet_key_refuses_without_touching_the_sdk(self, monkeypatch):
        _boom_order_builder(monkeypatch)
        v = PredictFunVenue()
        result = await v.redeem_positions("0xcond", [1])
        assert result == {
            "ok": False,
            "error": "Predict.fun wallet key not configured (PREDICT_WALLET_KEY)",
            "error_code": "no_wallet_key",
        }

    @pytest.mark.asyncio
    async def test_mainnet_without_live_enable_refuses(self, monkeypatch):
        _boom_order_builder(monkeypatch)
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        monkeypatch.setenv("PREDICT_TESTNET", "0")
        v = PredictFunVenue()
        result = await v.redeem_positions("0xcond", [1])
        assert result["ok"] is False
        assert result["error_code"] == "live_disabled"

    @pytest.mark.asyncio
    async def test_empty_index_sets_is_rejected_before_touching_the_sdk(self, monkeypatch):
        _boom_order_builder(monkeypatch)
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        v = PredictFunVenue()
        result = await v.redeem_positions("0xcond", [])
        assert result["ok"] is False
        assert "at least one index set" in result["error"]

    @pytest.mark.asyncio
    async def test_multiple_index_sets_is_refused_before_touching_the_sdk(self, monkeypatch):
        _boom_order_builder(monkeypatch)
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        v = PredictFunVenue()
        result = await v.redeem_positions("0xcond", [1, 2])
        assert result["ok"] is False
        assert "single index set" in result["error"]

    @pytest.mark.asyncio
    async def test_builds_the_right_sdk_call_and_extracts_the_tx_hash(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        tx_result = SimpleNamespace(success=True, receipt={"transactionHash": b"\x12\x34"})
        builder = Mock()
        builder.redeem_positions_async = AsyncMock(return_value=tx_result)
        fake_cls = _fake_order_builder(monkeypatch, builder)

        v = PredictFunVenue()
        result = await v.redeem_positions("0xcond123", [2])

        fake_cls.make.assert_called_once_with(v._sdk_chain_id, _TEST_KEY)
        builder.redeem_positions_async.assert_awaited_once_with(
            "0xcond123", 2, is_neg_risk=False, is_yield_bearing=False,
        )
        assert result == {"ok": True, "tx_hash": "1234"}

    @pytest.mark.asyncio
    async def test_failed_transaction_reports_the_cause(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        tx_result = SimpleNamespace(success=False, cause=RuntimeError("already redeemed"))
        builder = Mock()
        builder.redeem_positions_async = AsyncMock(return_value=tx_result)
        _fake_order_builder(monkeypatch, builder)

        v = PredictFunVenue()
        result = await v.redeem_positions("0xcond", [1])

        assert result["ok"] is False
        assert "already redeemed" in result["error"]

    @pytest.mark.asyncio
    async def test_sdk_exception_is_caught_and_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        fake_cls = Mock()
        fake_cls.make = Mock(side_effect=RuntimeError("rpc unreachable"))
        monkeypatch.setattr(pf_module, "OrderBuilder", fake_cls)

        v = PredictFunVenue()
        result = await v.redeem_positions("0xcond", [1])

        assert result["ok"] is False
        assert "rpc unreachable" in result["error"]


# ------------------------------------------------------------------------------------------------
# fetch_chain_shares_for_token()
# ------------------------------------------------------------------------------------------------

class _FakeCallResult:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _FakeFunctions:
    def __init__(self, seen, raw_balance=None, raise_on_call=None):
        self._seen = seen
        self._raw_balance = raw_balance
        self._raise_on_call = raise_on_call

    def balanceOf(self, address, token_id):  # noqa: N802 (mirrors the real web3 API)
        self._seen["address"] = address
        self._seen["token_id"] = token_id
        if self._raise_on_call is not None:
            class _Boom:
                def call(self_inner):
                    raise self._raise_on_call
            return _Boom()
        return _FakeCallResult(self._raw_balance)


class _FakeContract:
    def __init__(self, seen, *, address, abi, raw_balance=None, raise_on_call=None):
        seen["contract_address"] = address
        seen["abi"] = abi
        self.functions = _FakeFunctions(seen, raw_balance=raw_balance, raise_on_call=raise_on_call)


class _FakeEth:
    def __init__(self, seen, **contract_kwargs):
        self._seen = seen
        self._contract_kwargs = contract_kwargs

    def contract(self, address, abi):
        return _FakeContract(self._seen, address=address, abi=abi, **self._contract_kwargs)


def _install_fake_web3(monkeypatch, seen, **contract_kwargs):
    class _FakeWeb3:
        def __init__(self, provider):
            seen["rpc_provider"] = provider
            self.eth = _FakeEth(seen, **contract_kwargs)

        HTTPProvider = staticmethod(lambda url: seen.setdefault("rpc_url", url) or url)

        @staticmethod
        def to_checksum_address(addr):
            return addr

    monkeypatch.setattr(pf_module, "Web3", _FakeWeb3)


class TestFetchChainSharesForToken:
    @pytest.mark.asyncio
    async def test_no_wallet_key_returns_none_without_touching_web3(self, monkeypatch):
        _boom_web3(monkeypatch)
        v = PredictFunVenue()
        assert await v.fetch_chain_shares_for_token("45899948") is None

    @pytest.mark.asyncio
    async def test_reads_the_real_erc1155_balance_and_scales_by_1e18(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        seen: dict = {}
        _install_fake_web3(monkeypatch, seen, raw_balance=7_500_000_000_000_000_000)  # 7.5 shares

        v = PredictFunVenue()
        shares = await v.fetch_chain_shares_for_token("45899948")

        assert shares == pytest.approx(7.5)
        assert seen["address"] == _TEST_ADDR
        assert seen["token_id"] == 45899948  # str -> int conversion
        assert seen["contract_address"] == _TESTNET_CONDITIONAL_TOKENS
        assert seen["abi"] is pf_module.CONDITIONAL_TOKENS_ABI
        assert seen["rpc_url"] == "https://bsc-testnet-dataseed.bnbchain.org/"

    @pytest.mark.asyncio
    async def test_contract_read_failure_returns_none_not_raise(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        seen: dict = {}
        _install_fake_web3(monkeypatch, seen, raise_on_call=RuntimeError("rpc timeout"))

        v = PredictFunVenue()
        shares = await v.fetch_chain_shares_for_token("45899948")
        assert shares is None

    @pytest.mark.asyncio
    async def test_bad_token_id_returns_none_not_raise(self, monkeypatch):
        monkeypatch.setenv("PREDICT_WALLET_KEY", _TEST_KEY)
        seen: dict = {}
        _install_fake_web3(monkeypatch, seen, raw_balance=0)

        v = PredictFunVenue()
        shares = await v.fetch_chain_shares_for_token("not-a-number")
        assert shares is None
