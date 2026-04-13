"""
בדיקות יחידה: תמיכת proxy wallet (funder != signer) ב-live_clob.
כל הבדיקות ב-mock — אין קריאות רשת.
"""
import asyncio
import os
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

import live_clob


# ──────────────────── helpers ────────────────────


def _make_mock_client(signer="0xSIGNER", funder="0xFUNDER"):
    """יוצר mock של ClobClient עם signer/funder נפרדים."""
    client = MagicMock()
    client.get_address.return_value = signer
    client.funder = funder
    # builder.funder fallback
    client.builder = MagicMock()
    client.builder.funder = funder
    client.get_balance_allowance.return_value = {"balance": "5000000", "allowance": "10000000"}
    return client


# ──────────────────── fetch_polymarket_clob_account ────────────────────


def test_clob_account_exposes_funder():
    """תגובת clob account חייבת לכלול funder_address ו-is_proxy."""
    client = _make_mock_client(signer="0xAAA", funder="0xBBB")
    with patch.object(live_clob, "build_trading_client", return_value=(client, None)):
        result = live_clob.fetch_polymarket_clob_account()

    assert result["ok"] is True
    assert result["funder_address"] == "0xBBB"
    assert result["address"] == "0xAAA"
    assert result["is_proxy"] is True


def test_clob_account_eoa_not_proxy():
    """כש-funder == signer → is_proxy=False."""
    client = _make_mock_client(signer="0xAAA", funder="0xAAA")
    with patch.object(live_clob, "build_trading_client", return_value=(client, None)):
        result = live_clob.fetch_polymarket_clob_account()

    assert result["ok"] is True
    assert result["is_proxy"] is False


def test_clob_account_sub_dollar_micro_balance():
    """יתרה < 1$ ב-micro-USDC (למשל 500_000 = 0.50$) — לא להציג מספר גולמי מנופח."""
    client = _make_mock_client(signer="0xAAA", funder="0xAAA")
    client.get_balance_allowance.return_value = {"balance": "500000", "allowance": "1000000"}
    with patch.object(live_clob, "build_trading_client", return_value=(client, None)):
        result = live_clob.fetch_polymarket_clob_account()

    assert result["ok"] is True
    assert result["balance_usd"] == 0.5
    assert result["allowance_usd"] == 1.0


def test_clob_account_case_insensitive_proxy():
    """השוואת כתובות case-insensitive — 0xaaa == 0xAAA."""
    client = _make_mock_client(signer="0xaaa", funder="0xAAA")
    with patch.object(live_clob, "build_trading_client", return_value=(client, None)):
        result = live_clob.fetch_polymarket_clob_account()

    assert result["is_proxy"] is False


# ──────────────────── fetch_live_portfolio ────────────────────


@pytest.mark.asyncio
async def test_portfolio_uses_funder_for_positions():
    """כש-funder != signer, פוזיציות נמשכות לפי funder (שם הכסף)."""
    client = _make_mock_client(signer="0xSIGNER", funder="0xFUNDER")

    with (
        patch.object(live_clob, "build_trading_client", return_value=(client, None)),
        patch.object(live_clob, "fetch_live_positions", new_callable=AsyncMock, return_value=[]) as mock_pos,
    ):
        live_clob.reset_portfolio_cache()
        result = await live_clob.fetch_live_portfolio(force=True)

    mock_pos.assert_called_once_with("0xFUNDER")
    assert result["ok"] is True
    assert result["funder_address"] == "0xFUNDER"
    assert result["is_proxy"] is True


@pytest.mark.asyncio
async def test_portfolio_eoa_uses_signer():
    """EOA (funder == signer) — פוזיציות נמשכות לפי signer, ללא regression."""
    client = _make_mock_client(signer="0xEOA", funder="0xEOA")

    with (
        patch.object(live_clob, "build_trading_client", return_value=(client, None)),
        patch.object(live_clob, "fetch_live_positions", new_callable=AsyncMock, return_value=[]) as mock_pos,
    ):
        live_clob.reset_portfolio_cache()
        result = await live_clob.fetch_live_portfolio(force=True)

    mock_pos.assert_called_once_with("0xEOA")
    assert result["is_proxy"] is False


@pytest.mark.asyncio
async def test_zero_balance_hint_for_default_sig_type():
    """כש-balance=0, אין פוזיציות, ו-sig_type=0 → hint חייב להופיע."""
    client = _make_mock_client(signer="0xAAA", funder="0xAAA")
    client.get_balance_allowance.return_value = {"balance": "0", "allowance": "0"}

    env = {
        "POLYMARKET_PRIVATE_KEY": "0xfake",
        "POLYMARKET_SIGNATURE_TYPE": "0",
    }
    with (
        patch.object(live_clob, "build_trading_client", return_value=(client, None)),
        patch.object(live_clob, "fetch_live_positions", new_callable=AsyncMock, return_value=[]),
        patch.dict(os.environ, env, clear=False),
    ):
        live_clob.reset_portfolio_cache()
        result = await live_clob.fetch_live_portfolio(force=True)

    assert result["ok"] is True
    assert result["hint"] is not None
    assert "POLYMARKET_SIGNATURE_TYPE" in result["hint"]


@pytest.mark.asyncio
async def test_hint_deposit_when_sig_type_set():
    """כש-sig_type=1 ו-balance=0, מציגים hint על הפקדה (לא על SIGNATURE_TYPE)."""
    client = _make_mock_client(signer="0xAAA", funder="0xBBB")
    client.get_balance_allowance.return_value = {"balance": "0", "allowance": "0"}

    env = {
        "POLYMARKET_PRIVATE_KEY": "0xfake",
        "POLYMARKET_SIGNATURE_TYPE": "1",
    }
    with (
        patch.object(live_clob, "build_trading_client", return_value=(client, None)),
        patch.object(live_clob, "fetch_live_positions", new_callable=AsyncMock, return_value=[]),
        patch.dict(os.environ, env, clear=False),
    ):
        live_clob.reset_portfolio_cache()
        result = await live_clob.fetch_live_portfolio(force=True)

    assert result["ok"] is True
    assert result["hint"] is not None
    assert "POLYMARKET_SIGNATURE_TYPE" not in result["hint"]
    assert "CLOB" in result["hint"]


@pytest.mark.asyncio
async def test_no_hint_when_balance_positive():
    """כש-balance > 0, אין hint — הכל תקין."""
    client = _make_mock_client(signer="0xAAA", funder="0xAAA")
    client.get_balance_allowance.return_value = {"balance": "5000000", "allowance": "10000000"}

    env = {
        "POLYMARKET_PRIVATE_KEY": "0xfake",
        "POLYMARKET_SIGNATURE_TYPE": "0",
    }
    with (
        patch.object(live_clob, "build_trading_client", return_value=(client, None)),
        patch.object(live_clob, "fetch_live_positions", new_callable=AsyncMock, return_value=[]),
        patch.dict(os.environ, env, clear=False),
    ):
        live_clob.reset_portfolio_cache()
        result = await live_clob.fetch_live_portfolio(force=True)

    assert result["ok"] is True
    assert result["hint"] is None
