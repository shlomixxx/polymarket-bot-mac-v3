#!/usr/bin/env python3
"""scripts/predict_testnet_fund_and_prepare.py

ONE-COMMAND testnet readiness script for Predict.fun (M2b on-chain completion). Run this ONCE the
testnet wallet has been funded with tBNB (from a BNB testnet faucet) to get it fully ready to place
FILLED (not just signed) testnet orders:

  1. Check the wallet has tBNB gas (exits with clear instructions if not — nothing else can happen
     without gas).
  2. Mint 5,000 test USDT (TUSD) via the testnet collateral's PUBLIC `allocateTo(address,uint256)`
     faucet mint. This was manually verified callable against the real testnet contract before this
     script was written (see the M2b on-chain completion report) — it is not part of the standard
     ERC20 interface, hence the small dedicated ABI below instead of predict_sdk.ERC20_ABI.
  3. Run PredictFunVenue.ensure_approvals() so the CTF exchange (+ neg-risk exchange/adapter) is
     allowed to move that USDT and any outcome tokens.
  4. Print a plain readiness summary.

Safe to re-run: every step here is idempotent or harmless to repeat — re-minting just tops up more
test USDT, and ensure_approvals() skips any approval already granted on-chain (see its docstring in
engine/venues/predict_fun.py). Nothing here ever touches mainnet or a real (non-test) asset.

NEVER prints the private key — only the wallet's public address.

Usage:
    python3 scripts/predict_testnet_fund_and_prepare.py

Wallet key source (first match wins):
  1. env PREDICT_WALLET_KEY
  2. the JSON file at _WALLET_FALLBACK_FILE below (shape: {"address": "0x...", "private_key": "0x..."})
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# --- make engine/'s bare-module imports (predict_secrets, venues.predict_fun) resolve, exactly
# like engine/tests/conftest.py does for the test suite. Must happen before those imports below. ---
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENGINE_DIR = _REPO_ROOT / "engine"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

# Session-scratchpad fallback — a convenience for the machine/session this was built on. A real
# deployment should set PREDICT_WALLET_KEY instead of relying on this path existing.
_WALLET_FALLBACK_FILE = Path(
    "/private/tmp/claude-501/-Users-shlomishemtov-Documents-cursor-project-polymarket-bot-mac-v3"
    "/5eb43e1e-8a67-4d78-9774-a0166ca67b3a/scratchpad/predict_testnet_wallet.json"
)

_TUSD_ADDRESS = "0xB32171ecD878607FFc4F8FC0bCcE6852BB3149E0"  # testnet collateral (TUSD, 18-dec)
_RPC_URL = "https://bsc-testnet-dataseed.bnbchain.org/"
_MINT_AMOUNT_WEI = 5000 * 10**18  # 5,000 test USDT per run

# Minimal ABI: only what this script needs. `allocateTo` is a testnet-only faucet mint, not part
# of the standard ERC20 interface predict_sdk.ERC20_ABI covers.
_TUSD_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
        ],
        "name": "allocateTo", "outputs": [], "stateMutability": "nonpayable", "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
]


def _load_wallet_key() -> tuple[str, str]:
    """Returns (private_key, source_description), or exits with clear instructions if no key is
    found anywhere. Callers must not print the returned key — only pass it along."""
    env_key = (os.environ.get("PREDICT_WALLET_KEY") or "").strip()
    if env_key:
        return env_key, "env PREDICT_WALLET_KEY"
    if _WALLET_FALLBACK_FILE.exists():
        try:
            data = json.loads(_WALLET_FALLBACK_FILE.read_text())
            key = (data.get("private_key") or "").strip()
            if key:
                return key, f"fallback file {_WALLET_FALLBACK_FILE.name}"
        except Exception as e:
            print(f"(could not read the fallback wallet file {_WALLET_FALLBACK_FILE}: {e})")
    print("No Predict.fun wallet key found.")
    print("Set env PREDICT_WALLET_KEY to a 0x-prefixed private key, or place one in a JSON file at:")
    print(f"  {_WALLET_FALLBACK_FILE}")
    print('  (shape: {"address": "0x...", "private_key": "0x..."})')
    sys.exit(1)


async def main() -> None:
    print("=" * 70)
    print("Predict.fun testnet — fund & prepare")
    print("=" * 70)

    key, source = _load_wallet_key()
    # So predict_secrets/PredictFunVenue see the same key and stay on the default-safe testnet
    # regardless of whatever the calling shell happened to have set.
    os.environ["PREDICT_WALLET_KEY"] = key
    os.environ.pop("PREDICT_TESTNET", None)
    account = Account.from_key(key)
    address = account.address
    print(f"Wallet key source : {source}")
    print(f"Wallet address    : {address}")
    print()

    w3 = Web3(Web3.HTTPProvider(_RPC_URL))

    # --- Step 1/4: tBNB gas check ---
    print("Step 1/4 — checking tBNB gas balance...")
    bnb_wei = w3.eth.get_balance(address)
    bnb = bnb_wei / 1e18
    print(f"  tBNB balance: {bnb:.6f} tBNB")
    if bnb_wei == 0:
        print()
        print(f"NO GAS — fund {address} from a BNB testnet faucet first, then re-run this script.")
        print("  e.g. https://www.bnbchain.org/en/testnet-faucet")
        sys.exit(1)
    print("  OK - wallet has gas.")
    print()

    # --- Step 2/4: mint test USDT ---
    print("Step 2/4 — minting test USDT (TUSD)...")
    tusd = w3.eth.contract(address=Web3.to_checksum_address(_TUSD_ADDRESS), abi=_TUSD_ABI)
    try:
        fn = tusd.functions.allocateTo(address, _MINT_AMOUNT_WEI)
        estimated_gas = fn.estimate_gas({"from": address})
        gas_limit = (estimated_gas * 125) // 100
        tx = fn.build_transaction({
            "from": address,
            "nonce": w3.eth.get_transaction_count(address, "pending"),
            "gas": gas_limit,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  mint tx sent: {tx_hash.hex()} — waiting for confirmation...")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            print("  WARNING — mint transaction reverted. Continuing to check balances anyway.")
        else:
            print("  OK - mint confirmed.")
    except Exception as e:
        print(f"  WARNING — mint failed ({e}). Continuing (wallet may already hold enough TUSD).")
    tusd_balance_wei = tusd.functions.balanceOf(address).call()
    tusd_balance = tusd_balance_wei / 1e18
    print(f"  TUSD balance now: {tusd_balance:.4f} TUSD")
    print()

    # --- Step 3/4: on-chain approvals ---
    print("Step 3/4 — running on-chain approvals (CTF exchange + neg-risk exchange/adapter)...")
    from venues.predict_fun import PredictFunVenue
    venue = PredictFunVenue()
    approvals = await venue.ensure_approvals()
    if approvals.get("ok"):
        print(f"  OK - approvals in place ({approvals.get('steps_run', 0)} step(s) sent).")
    else:
        print(f"  FAILED — {approvals.get('error')}")
    print()

    # --- Step 4/4: readiness summary ---
    print("Step 4/4 — readiness summary")
    print("-" * 70)
    print(f"  tBNB balance : {bnb:.6f} tBNB")
    print(f"  TUSD balance : {tusd_balance:.4f} TUSD")
    approvals_line = "OK" if approvals.get("ok") else f"FAILED — {approvals.get('error')}"
    print(f"  Approvals    : {approvals_line}")
    print("-" * 70)
    if bnb_wei > 0 and tusd_balance > 0 and approvals.get("ok"):
        print("READY — the bot can now place filled testnet orders on Predict.fun.")
    else:
        print("NOT READY YET — see the failures above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
