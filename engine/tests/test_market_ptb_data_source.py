from __future__ import annotations

import main


def test_binance_mode_prefers_binance_ptb():
    # helper טהור: בהינתן מקור="binance" + open של Binance, בוחר binance_1m ולא chainlink.
    val, src = main._resolve_ptb_for_source(
        active="binance", binance_open=100_000.0, chainlink_ptb=99_950.0,
    )
    assert (val, src) == (100_000.0, "binance_1m")


def test_binance_mode_pending_when_no_open():
    val, src = main._resolve_ptb_for_source(
        active="binance", binance_open=None, chainlink_ptb=99_950.0,
    )
    assert (val, src) == (None, "pending")


def test_polymarket_mode_prefers_chainlink_ptb():
    val, src = main._resolve_ptb_for_source(
        active="polymarket", binance_open=100_000.0, chainlink_ptb=99_950.0,
    )
    assert (val, src) == (99_950.0, "chainlink_stream")


def test_polymarket_mode_falls_back_to_binance_when_no_chainlink():
    val, src = main._resolve_ptb_for_source(
        active="polymarket", binance_open=100_000.0, chainlink_ptb=None,
    )
    assert (val, src) == (100_000.0, "binance_1m_fallback")


def test_polymarket_mode_pending_when_nothing_available():
    val, src = main._resolve_ptb_for_source(
        active="polymarket", binance_open=None, chainlink_ptb=None,
    )
    assert (val, src) == (None, "pending")
