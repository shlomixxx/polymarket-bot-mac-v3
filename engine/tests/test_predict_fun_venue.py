# engine/tests/test_predict_fun_venue.py
"""PredictFunVenue — M2a read-only (discover + book + best_bid_ask). Orders raise until M2b.

NOTE ON THE MOCK SHAPE: the task brief's sketch used bare floats for outcomes[].bestBid/bestAsk
(e.g. 0.12). A live testnet capture (see m2-research-market-map.md §1.3) shows the REAL shape is
either `null` (no liquidity yet) or a `{"price": <0..1>, "size": <shares>}` object — never a bare
float. The mock below uses the REAL nested shape; PredictFunVenue._price_of() normalizes it (and,
defensively, still accepts a bare number) so a shape drift doesn't silently break parsing.
"""
from __future__ import annotations
from unittest.mock import AsyncMock, patch
import pytest
from venues.predict_fun import PredictFunVenue

# a market object shaped like GET /v1/markets?marketVariant=CRYPTO_UP_DOWN (outcomes REVERSED on
# purpose — array order is not guaranteed; mapping MUST go by outcome name, not index).
_MARKET = {
    "id": 778011, "conditionId": "0x40c806", "categorySlug": "btc-updown-5m-1700000100",
    "status": "OPEN", "tradingStatus": "OPEN", "feeRateBps": 200,
    "outcomes": [
        {"indexSet": 2, "name": "Down", "onChainId": "24946845",
         "bestBid": {"price": 0.12, "size": 445470.0}, "bestAsk": {"price": 0.13, "size": 391793.66}},
        {"indexSet": 1, "name": "Up", "onChainId": "45899948",
         "bestBid": {"price": 0.87, "size": 391793.66}, "bestAsk": {"price": 0.88, "size": 445470.0}},
    ],
}


def test_identity_props():
    v = PredictFunVenue()
    assert (v.name, v.is_testnet, v.collateral, v.chain_id) == ("predict_fun", True, "USDT", 97)


@pytest.mark.asyncio
async def test_discover_maps_up_down_by_name_not_index():
    v = PredictFunVenue()
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[_MARKET])):
        m = await v.discover_active_window("5m")
    assert m is not None
    assert m.token_up == "45899948"     # the outcome NAMED "Up", despite being 2nd in the array
    assert m.token_down == "24946845"   # the outcome NAMED "Down", despite being 1st in the array
    assert m.window_sec == 300
    assert m.condition_id == "0x40c806"


@pytest.mark.asyncio
async def test_discover_15m_window_sec_and_missing_outcome_returns_none():
    v = PredictFunVenue()
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[_MARKET])):
        m15 = await v.discover_active_window("15m")   # slug is a 5m slug -> no 15m match
    assert m15 is None

    one_sided = {**_MARKET, "outcomes": [_MARKET["outcomes"][0]]}  # only "Down" present
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[one_sided])):
        m = await v.discover_active_window("5m")
    assert m is None


@pytest.mark.asyncio
async def test_best_bid_ask_reads_nested_price_by_onchainid_not_index():
    v = PredictFunVenue()
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[_MARKET])):
        up_bid, up_ask = await v.best_bid_ask("45899948")     # "Up" onChainId
        down_bid, down_ask = await v.best_bid_ask("24946845")  # "Down" onChainId
    assert (up_bid, up_ask) == (0.87, 0.88)
    assert (down_bid, down_ask) == (0.12, 0.13)


@pytest.mark.asyncio
async def test_best_bid_ask_unknown_token_returns_none_none():
    v = PredictFunVenue()
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[_MARKET])):
        bid, ask = await v.best_bid_ask("does-not-exist")
    assert (bid, ask) == (None, None)


@pytest.mark.asyncio
async def test_get_book_mirrors_down_side_from_yes_priced_orderbook():
    # Full-depth orderbook (GET /v1/markets/{id}/orderbook) is Yes(Up)-priced only per the docs
    # and the live sample (m2-research-market-map.md §2.1): "YES asks == NO bids, YES bids == NO
    # asks". So the Down-side book must be derived as (1 - price), not read directly.
    v = PredictFunVenue()
    yes_book = {"bids": [[0.03, 445470.0]], "asks": [[0.04, 391793.66]]}
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[_MARKET])), \
         patch.object(v, "_get_orderbook", AsyncMock(return_value=yes_book)):
        up_book = await v.get_book(None, "45899948")      # Up: pass-through
        down_book = await v.get_book(None, "24946845")     # Down: mirrored at (1 - price)
    assert up_book == {"bids": [{"price": 0.03, "size": 445470.0}],
                        "asks": [{"price": 0.04, "size": 391793.66}]}
    assert down_book["bids"] == [{"price": 0.96, "size": 391793.66}]   # 1 - 0.04 (the Yes ask)
    assert down_book["asks"] == [{"price": 0.97, "size": 445470.0}]    # 1 - 0.03 (the Yes bid)


@pytest.mark.asyncio
async def test_get_book_unknown_token_returns_empty_book():
    v = PredictFunVenue()
    with patch.object(v, "_get_open_crypto_updown_markets", AsyncMock(return_value=[_MARKET])):
        book = await v.get_book(None, "does-not-exist")
    assert book == {"bids": [], "asks": []}


@pytest.mark.asyncio
async def test_order_methods_raise_notimplemented_in_m2a():
    v = PredictFunVenue()
    with pytest.raises(NotImplementedError):
        await v.place_entry_order("T", 10, 0.5, "Up")
    with pytest.raises(NotImplementedError):
        await v.place_exit_order("T", 10, 0.5)


@pytest.mark.asyncio
async def test_portfolio_and_account_return_benign_m2b_placeholder_not_real_data():
    v = PredictFunVenue()
    portfolio = await v.fetch_portfolio()
    assert portfolio["ok"] is False
    assert portfolio["balance_usd"] == 0.0
    account = v.fetch_account()
    assert account["ok"] is False
    assert await v.fetch_chain_shares_for_token("anything") is None
    assert v.reset_caches() is None


def test_live_disabled_reason_delegates_to_predict_secrets_triple_lock(monkeypatch):
    # M2b: live_disabled_reason() now delegates to predict_secrets.live_disabled_reason()
    # (PREDICT_LIVE + PREDICT_WALLET_KEY + not PREDICT_TESTNET) instead of an inline,
    # partial (PREDICT_LIVE + PREDICT_PRIVATE_KEY) check — see test_predict_secrets.py
    # for the full truth table; this just proves the delegation is wired.
    v = PredictFunVenue()
    for var in ("PREDICT_LIVE", "PREDICT_WALLET_KEY", "PREDICT_TESTNET"):
        monkeypatch.delenv(var, raising=False)
    assert v.live_disabled_reason() == "PREDICT_LIVE != '1'"  # no PREDICT_LIVE => disabled

    monkeypatch.setenv("PREDICT_LIVE", "1")
    assert v.live_disabled_reason() == "אין מפתח ארנק Predict.fun"  # PREDICT_LIVE=1 but no key

    monkeypatch.setenv("PREDICT_WALLET_KEY", "0xabc")
    assert v.live_disabled_reason() == "מצב טסטנט (PREDICT_TESTNET)"  # key set, still testnet by default

    monkeypatch.setenv("PREDICT_TESTNET", "0")
    assert v.live_disabled_reason() is None  # all three satisfied => real trading enabled
