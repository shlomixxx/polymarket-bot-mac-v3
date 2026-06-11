"""
binance_secrets.py — persistent, NEVER-logged storage for the Binance USDⓈ-M
Futures API key + secret of the responsible MANUAL-TRADING COCKPIT.

This is a thin clone of secret_store.py scoped to its OWN keyring service
(`binance-futures-bot`), so the Binance keys and the Polymarket private key can
NEVER collide in the same store. It reuses secret_store's keyring→chmod-600-file
fallback (works on macOS Keychain locally and on a Railway Volume in prod).

NON-NEGOTIABLE:
  * The key + secret are NEVER printed, logged, or returned in any API response.
    Only booleans about their PRESENCE are ever exposed (see is_live_enabled /
    has_keys). This module deliberately exposes no "reveal" helper.
  * They are stored as a single newline-joined blob "KEY\\nSECRET" under one
    secret_store entry (matching how binance_exchange._build_real_client reads
    it back), so the real connector can be constructed without env vars.
  * is_live_enabled() is the SINGLE source of truth the API uses to decide
    whether a REAL (live) order may be placed: env BINANCE_LIVE=='1' AND keys
    present AND we are NOT pointed at testnet. Anything short of all three keeps
    the cockpit on testnet — a real order is NEVER placed silently.

The API keys themselves must be created as futures-trade-only with WITHDRAWALS
OFF (documented for the owner in the cockpit UI); this module only stores them.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import secret_store

_log = logging.getLogger(__name__)

# Dedicated keyring service — distinct from secret_store.SERVICE ("polymarket-bot")
# so the two key stores can never collide. Mirrors binance_exchange.SECRET_SERVICE.
SERVICE = "binance-futures-bot"

# We join the two secrets with a newline into one stored blob. The secret itself
# is base64-ish/hex and never contains a newline, so this split is unambiguous.
_SEP = "\n"


def _env_keys() -> tuple[Optional[str], Optional[str]]:
    """Read keys from the environment (BINANCE_API_KEY / BINANCE_API_SECRET).
    Env takes precedence over the persisted store, matching binance_exchange."""
    k = (os.environ.get("BINANCE_API_KEY") or "").strip()
    s = (os.environ.get("BINANCE_API_SECRET") or "").strip()
    return (k or None, s or None)


def save_keys(api_key: str, api_secret: str) -> bool:
    """Persist the Binance futures key + secret under the dedicated service.

    Stored as a single "KEY\\nSECRET" blob (the shape binance_exchange reads
    back). Returns True on success. Never logs the values. Refuses blanks."""
    k = (api_key or "").strip()
    s = (api_secret or "").strip()
    if not k or not s:
        _log.warning("save_keys refused: empty api_key or api_secret")
        return False
    if _SEP in k or _SEP in s:
        # Defensive: a newline would corrupt the KEY\nSECRET split on read-back.
        _log.warning("save_keys refused: key/secret contained a newline")
        return False
    blob = f"{k}{_SEP}{s}"
    try:
        return bool(secret_store.save_key(blob, service=SERVICE))
    except TypeError:
        # An older secret_store without a `service` param must NOT fall back to
        # the polymarket-scoped store (the two must never collide). Refuse.
        _log.error(
            "secret_store.save_key() has no `service` param; cannot scope to %s — "
            "set BINANCE_API_KEY/SECRET env instead", SERVICE,
        )
        return False
    except Exception as exc:
        _log.warning("save_keys failed: %r", exc)
        return False


def load_keys() -> tuple[Optional[str], Optional[str]]:
    """Return (api_key, api_secret) or (None, None) if not available.

    Order: env first (BINANCE_API_KEY/SECRET), then the persisted store. Never
    logs/returns the values anywhere they could leak — callers in main.py only
    pass them straight into the connector and never echo them back."""
    env_k, env_s = _env_keys()
    if env_k and env_s:
        return (env_k, env_s)
    try:
        blob = secret_store.load_key(service=SERVICE)
    except TypeError:
        _log.error(
            "secret_store.load_key() has no `service` param; cannot scope to %s — "
            "set BINANCE_API_KEY/SECRET env instead", SERVICE,
        )
        return (None, None)
    except Exception as exc:
        _log.warning("load_keys failed: %r", exc)
        return (None, None)
    if blob and _SEP in blob:
        k, s = blob.split(_SEP, 1)
        k, s = k.strip(), s.strip()
        if k and s:
            return (k, s)
    return (None, None)


def has_keys() -> bool:
    """True iff BOTH a key and a secret are available (env or store).
    Exposes only presence, never the values."""
    k, s = load_keys()
    return bool(k and s)


def delete_keys() -> bool:
    """Remove the persisted Binance keys (env vars are NOT touched). True if a
    stored blob existed and was removed."""
    try:
        return bool(secret_store.delete_key(service=SERVICE))
    except TypeError:
        _log.error("secret_store.delete_key() has no `service` param")
        return False
    except Exception as exc:
        _log.warning("delete_keys failed: %r", exc)
        return False


def is_testnet() -> bool:
    """Whether the cockpit is pointed at the Binance TESTNET.

    DEFAULT IS TESTNET (safe). Only an explicit USE_TESTNET in {0,false,no,off}
    flips us to LIVE endpoints; anything else (unset, '1', 'true', …) stays on
    testnet. This default-safe posture means a missing/garbled env can never
    accidentally aim real orders at the live exchange."""
    raw = (os.environ.get("USE_TESTNET") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def is_live_enabled() -> bool:
    """THE single gate for whether a REAL (live-money) order may be placed.

    All three must hold:
      1. env BINANCE_LIVE == '1'  (the deploy kill-switch, mirrors POLYMARKET_LIVE)
      2. keys are present          (env or persisted store)
      3. we are NOT on testnet     (USE_TESTNET not in {0,false,no,off})

    If any is false, the cockpit must run against TESTNET or refuse — never place
    a real order silently. Returns a plain bool; reveals nothing about the keys.
    """
    live_flag = (os.environ.get("BINANCE_LIVE") or "0").strip() == "1"
    return bool(live_flag and has_keys() and not is_testnet())


def live_status() -> dict[str, object]:
    """A SAFE, key-free status dict for /api/binance/state. Returns only booleans
    + a human reason; NEVER any portion of the key/secret."""
    live_flag = (os.environ.get("BINANCE_LIVE") or "0").strip() == "1"
    testnet = is_testnet()
    keys = has_keys()
    enabled = bool(live_flag and keys and not testnet)
    reason = None
    if not enabled:
        if not live_flag:
            reason = "BINANCE_LIVE != '1' (deploy kill-switch) — running on testnet"
        elif not keys:
            reason = "no Binance API keys stored — running on testnet"
        elif testnet:
            reason = "USE_TESTNET is on — running on testnet"
    return {
        "live_enabled": enabled,
        "binance_live_flag": live_flag,
        "testnet": testnet,
        "has_keys": keys,
        "reason_blocked": reason,
    }
