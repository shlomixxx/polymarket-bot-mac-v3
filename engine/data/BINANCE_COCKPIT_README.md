# Binance USDⓈ-M Futures — Manual-Trading Cockpit (owner guide)

This is a **responsible manual-trading cockpit** for your **real** Binance USDⓈ-M
Futures account. **You make every trade decision.** The tool's only job is
**safety enforcement + transparency** — it does NOT pick trades, predict price, or
claim an edge. It will, however, refuse to let you do the dangerous things:
no order without a verified stop, no over-sizing, no martingale.

> **Defaults are safe.** Out of the box the cockpit runs on **TESTNET** with
> **live disabled**. It can never place a real order until you deliberately do
> all three things in the "Go live" section below.

---

## 1. Create the Binance API key (the SECURE way)

Do this on the real Binance site (or the testnet site first — see §3).

1. **API Management → Create API** → choose **System generated** keys.
2. **ENABLE** only: **"Enable Futures"**. That is the *only* permission this
   cockpit needs.
3. **DISABLE** everything else. Critically:
   - **Withdrawals: OFF** (leave "Enable Withdrawals" unchecked). The cockpit
     code has *no* withdraw/transfer method at all — but turn it off on the key
     too, so even a stolen key cannot move your money.
   - **Internal Transfer / Universal Transfer: OFF.**
   - Spot & Margin Trading: not needed — leave OFF.
4. **Restrict access to trusted IPs only (IP allowlist).** Add the IP of the
   machine/server that runs the cockpit. An un-allowlisted key is far more
   dangerous if it leaks.
5. Copy the **API Key** and **Secret Key**. The secret is shown **once** — if you
   lose it, delete the key and make a new one.

**Never paste the key or secret into a chat, a commit, a screenshot, or a log.**
The cockpit never prints, logs, or returns your keys — and you shouldn't either.

---

## 2. Give the cockpit your keys (two supported ways)

The cockpit reads keys from **environment variables first**, then from a secure
local store (`secret_store`: OS keychain, or a `chmod 600` file on the Railway
`/data` volume). The keys are stored under their **own** service name
(`binance-futures-bot`) so they can never collide with the Polymarket key.

**Option A — environment variables (simplest for a server/Railway):**

```bash
export BINANCE_API_KEY="your-key-here"
export BINANCE_API_SECRET="your-secret-here"
```

On Railway: add `BINANCE_API_KEY` and `BINANCE_API_SECRET` as **service
variables** (they live in Railway's secret store, not in the repo).

**Option B — the local secret store (macOS Keychain / `/data` file):**

```bash
python3 - <<'PY'
import binance_secrets
ok = binance_secrets.save_keys("your-key-here", "your-secret-here")
print("stored:", ok)        # prints True/False only — never the key
PY
```

This writes to the OS keychain if available, otherwise to a `chmod 600` file on
the `DATA_ROOT` volume (`.binance-futures-bot_pk`). Both are git-ignored.
To check presence without revealing anything: `binance_secrets.has_keys()` → bool.
To remove: `binance_secrets.delete_keys()`.

> The keys are **never** sent over the API. There is **no** endpoint that accepts
> or returns them. `GET /api/binance/state` exposes only booleans
> (`has_keys`, `live_enabled`, `testnet`) — never key material.

---

## 3. Start on TESTNET first (no real money)

TESTNET is the **default**. You only need testnet keys (make them at
<https://testnet.binancefuture.com> the same safe way as §1) and:

```bash
export USE_TESTNET="true"     # default-safe; this is the out-of-the-box state
unset  BINANCE_LIVE           # or leave it != "1"
export BINANCE_API_KEY="...testnet key..."
export BINANCE_API_SECRET="...testnet secret..."
# X-Bot-Token guards every write endpoint — set it even on testnet:
export BOT_API_TOKEN="a-long-random-string"
```

Then start the server as usual. The cockpit talks to
`https://testnet.binancefuture.com`. Every `/api/binance/trade` response is
labelled `"live_enabled": false` and notes that the order ran on **testnet, not
real money**. Place a few practice trades and confirm:
- the **preview** shows the real net cost (fees + slippage) and every safety check,
- a real **stop** appears on the exchange the instant a position opens,
- the **🐞 Faults** tab stays empty (a naked-stop fault means something is wrong).

---

## 4. Go live (REAL money) — the deliberate three-step gate

A real order is placed **only when ALL THREE are true** (any one missing → stays
on testnet, never silently live):

1. **`BINANCE_LIVE=1`** — the deploy kill-switch (mirrors `POLYMARKET_LIVE`).
2. **Keys are present** (env or secret store), and
3. **`USE_TESTNET` is off** — set it to `0`/`false`/`no`/`off`.

Plus, every write endpoint still requires the **`X-Bot-Token`** header matching
`BOT_API_TOKEN`. So:

```bash
export BINANCE_API_KEY="...REAL key..."
export BINANCE_API_SECRET="...REAL secret..."
export USE_TESTNET="false"          # off → live endpoints (fapi.binance.com)
export BINANCE_LIVE="1"             # the live kill-switch ON
export BOT_API_TOKEN="a-long-random-string"   # required for /trade and /close
```

Confirm it took effect: `GET /api/binance/state` should report
`"live_enabled": true`, `"testnet": false`. To **instantly disable real trading**
again, set `BINANCE_LIVE=0` (or `USE_TESTNET=true`) and restart — the gate
flips back to testnet immediately.

> **On Railway, `BINANCE_LIVE` resets to off on restart unless set as a service
> variable.** That is intentional: a redeploy can never leave you accidentally
> live.

---

## 5. What the cockpit guarantees (so you don't have to)

- **One approve path.** Every order — yours included — is sized and gated by
  `risk_engine.gate_order`: fixed-fractional sizing (the stop sets the size),
  ≥2:1 reward:risk, ≤3× effective leverage, ≤2% risk/trade hard ceiling, and the
  daily −3% / global −10% loss caps. A rejected gate places **zero** orders.
- **Always-stopped.** The stop-loss is placed on the exchange **atomically** with
  entry (ALGO order, `closePosition=true`, `MARK_PRICE`, `priceProtect`) and then
  **verified live**. If it can't be verified → the position is **auto market-
  closed** and a loud fault is recorded. You are never left with naked leverage.
- **Reconcile on boot.** On startup the cockpit scans your open positions; any
  without a live stop gets one placed, or is flattened.
- **No martingale, ever.** Sizing depends only on *current* equity — there is no
  doubling, averaging-down, add-to-loser, or stop-widening code path.
- **Cannot move your money.** There is no withdraw/transfer method anywhere in the
  code (proven by tests). Keep withdrawals off on the key too (§1).
- **Honest numbers.** Previews show net P&L after real taker fees + slippage, and
  the liquidation price vs. your stop.
- **Audit trail.** Entries, exits, and faults are logged to a *separate* sidecar
  ledger `binance_audit.db` (mode `binance`) — it never touches the Polymarket
  demo/live audit DB.

---

## 6. If something looks wrong

- Open the **🐞 Faults** tab and the Binance cockpit tab in the UI.
- A `naked_position_guard` response (HTTP 409) means a stop could not be
  confirmed and the position was force-closed — read the message; in the rare
  case it says **"OPEN AND NAKED"**, the emergency close itself was rejected by
  the exchange: **flatten that position manually, now.**
- Rotate the key (delete + recreate per §1) if you ever suspect it leaked, then
  re-store it (§2). The cockpit holds no copy you need to scrub beyond
  `binance_secrets.delete_keys()`.
