"""
predict_secrets.py — dedicated triple-lock helper for real (live-money) Predict.fun
orders. A thin sibling of binance_secrets.py, scoped ONLY to Predict.fun's wallet key
so it can never collide with POLYMARKET_PRIVATE_KEY or the Binance API key/secret.

NON-NEGOTIABLE:
  * The wallet key itself is NEVER read/returned by this module. Only a boolean about
    its PRESENCE is ever exposed (has_wallet_key()). This module deliberately exposes
    no "reveal" helper and does not persist the key (that is a later M2b step, if ever
    needed) — for M2b-step-1 the key comes from env only.
  * is_live_enabled() is THE single source of truth strategy_runner._live_trading_ok()
    uses (when order_venue == "predict_fun") to decide whether a REAL Predict.fun order
    may be placed: env PREDICT_LIVE == '1' AND a wallet key is present AND we are NOT
    pointed at testnet. Anything short of all three keeps Predict.fun on testnet — a
    real order is NEVER placed silently.
  * Self-contained: this module imports nothing from live_clob/strategy_runner/venues,
    so it can be imported from anywhere without pulling in the trading engine.

Mirrors binance_secrets.py's three-lock shape:
  1. PREDICT_LIVE == '1'        (deploy kill-switch)
  2. wallet key present          (env PREDICT_WALLET_KEY)
  3. NOT pointed at testnet      (PREDICT_TESTNET not in {0,false,no,off})
"""
from __future__ import annotations

import os
from typing import Optional

_OFF_VALUES = ("0", "false", "no", "off")


def is_testnet() -> bool:
    """Whether Predict.fun trading is pointed at the BNB TESTNET (chain 97).

    DEFAULT IS TESTNET (safe). Only an explicit PREDICT_TESTNET in {0,false,no,off}
    flips us to the mainnet endpoint/chain; anything else (unset, '1', 'true', …)
    stays on testnet. This default-safe posture means a missing/garbled env can never
    accidentally aim real orders at mainnet."""
    raw = (os.environ.get("PREDICT_TESTNET") or "").strip().lower()
    if raw in _OFF_VALUES:
        return False
    return True


def has_wallet_key() -> bool:
    """True iff a Predict.fun wallet private key is available via env PREDICT_WALLET_KEY.
    Exposes only presence, never the value — this module has no "reveal" helper."""
    return bool((os.environ.get("PREDICT_WALLET_KEY") or "").strip())


def is_live_enabled() -> bool:
    """THE single gate for whether a REAL (live-money) Predict.fun order may be placed.

    All three must hold:
      1. env PREDICT_LIVE == '1'   (the deploy kill-switch, mirrors POLYMARKET_LIVE/BINANCE_LIVE)
      2. a wallet key is present    (env PREDICT_WALLET_KEY)
      3. we are NOT on testnet      (PREDICT_TESTNET not in {0,false,no,off})

    If any is false, Predict.fun trading must stay on testnet/fake money or refuse —
    never place a real order silently. Returns a plain bool; reveals nothing about the key.
    """
    live_flag = (os.environ.get("PREDICT_LIVE") or "0").strip() == "1"
    return bool(live_flag and has_wallet_key() and not is_testnet())


def live_disabled_reason() -> Optional[str]:
    """Names the FIRST open lock (same order as is_live_enabled), or None if all three
    are satisfied (real Predict.fun trading is enabled). A SAFE, key-free string — never
    any portion of the wallet key."""
    live_flag = (os.environ.get("PREDICT_LIVE") or "0").strip() == "1"
    if not live_flag:
        return "PREDICT_LIVE != '1'"
    if not has_wallet_key():
        return "אין מפתח ארנק Predict.fun"
    if is_testnet():
        return "מצב טסטנט (PREDICT_TESTNET)"
    return None
