# Strategy Discipline & Circuit-Breaker — Work Plan

- **Date:** 2026-06-05
- **Status:** PLAN ONLY — no engine/trading code was changed. Nothing here is deployed.
- **Origin:** The Trade Coach (Phase B) lessons on 1,457 real prod (demo) trades surfaced three critical issues — poor exit discipline, "green turned red", and an existential martingale tail. A read-only investigation of the engine then found that **most of the suggested fixes already exist as config knobs**; the only genuine *code* gap is a circuit-breaker.

> ⚠️ Important honesty note: the lessons came from **historical demo data generated under a PAST config**. Today's *default* config already has the peak-watchdog ON and hold-to-resolution OFF. So before over-tuning, **turn the bot on with the new build, let live (schema_version=1) trades accrue, and re-read the coach lessons** — then tune against current behavior, not the past.

---

## Part 1 — Config tuning (NO code; you set these in the dashboard "אסטרטגיה" tab)

These are **your** trading parameters — they already exist; nobody should change them silently. Recommended directions below, each tied to a lesson, with the existing field, its current default, and the trade-off. Apply conservatively, one at a time, and watch the result.

| Lesson | Existing config field | Current default | Suggested direction | Why / trade-off |
|---|---|---|---|---|
| Martingale = existential risk (#1 critical) | `loss_recovery_max_multiplier` (`strategy_runner.py:84`) | **10.0×** | Lower to **2–3×** (or disable martingale: `loss_recovery_enabled=false`) | Caps how big a recovery bet can get. 10× on a $5 base = $50 single bet after a streak; the data showed a −$10,519 single loss. This is the single highest-impact safety knob. |
| Martingale magnitude | `max_notional_per_window_usd` (`strategy_runner.py:71`) | **1,000,000** (≈unlimited) | Set a **real ceiling** (e.g. 5–10× your base investment) | Hard $ cap on total exposure per 5m/15m window — a backstop independent of the multiplier. |
| Exit discipline — TP not firing | `take_profit_pct` (`strategy_runner.py:53`) | **20.0%** | Consider **15–18%** | Lower threshold catches more of the fleeting spikes the data showed were left on the table. Trade-off: smaller average win, more frequent exits. |
| Green turned red — peak give-back | `peak_retreat_exit_pct` (`strategy_runner.py:97`, `peak_watchdog_enabled=true` by default) | **2.0%** | Consider **3–5%** | The peak-watchdog ALREADY locks profit on retreat (good!). 2% is twitchy on a volatile 5m binary; 3–5% holds the peak a bit longer before bailing. |
| Green turned red — DCA lock | `dca_tp_override_pct` (`strategy_runner.py`) | **50.0%** | If `dca_enabled=true`, lower to **~20%** | When DCA is mid-sequence, TP is locked until +50% gain; lowering lets a green trade take profit at +20% instead of riding to settlement. |
| Forced hold blocking TP | `hold_to_resolution_enabled` | **false** (default) | Keep **false** | If it was ever turned on, it blocks ALL take-profit exits (forces hold to settlement) — exactly the "green turned red" failure. Verify it's off. |

**Sequencing for Part 1:** the two martingale knobs (`loss_recovery_max_multiplier`, `max_notional_per_window_usd`) are the most important and the safest to tighten first. The TP/peak knobs are refinements — best tuned after you have live data under the new build.

---

## Part 2 — Circuit-breaker (the ONE code gap) — implementation plan

**What's missing (verified):** even when `loss_recovery_max_multiplier` (10×) is reached, the bot keeps entering at cap size **indefinitely**. There is NO halt-after-N-losses, no equity-floor, no halt-at-cap. The existing `loss_recovery_streak` counter (`demo_engine.py:65`, reset on win / ++ on loss in `loss_recovery.py:55,63`) is purely informational. A fault is *logged* at 80% of cap (`strategy_runner.py:1298-1313`) but nothing blocks entries.

**Design principle: opt-in, DEFAULT OFF.** Until you enable it, behavior is byte-for-byte unchanged — so it is safe to build, test, and ship without affecting the running bot.

### New StrategyConfig fields (all default to "disabled")
```python
circuit_breaker_enabled: bool = False                 # master switch
circuit_breaker_max_consecutive_losses: int = 0       # 0 = off; e.g. 5 = halt after 5 losses in a row
circuit_breaker_halt_at_cap: bool = False             # halt once the loss-recovery multiplier hits its cap
circuit_breaker_equity_floor_pct: float = 0.0         # 0 = off; halt if equity < this % of the session baseline
circuit_breaker_action: Literal["block_entries", "mode_off"] = "block_entries"
```
`_load_persisted_config` ignores unknown keys (`if hasattr`), so adding these is deploy-safe (per the prod-server notes).

### Behavior when tripped
- **Blocks NEW entries** (default) — or sets `mode="off"` if `circuit_breaker_action="mode_off"`.
- **Existing positions still exit normally** (TP / settlement are untouched — never trap an open position).
- Records a **critical fault** (`fault_tracker.record_fault`) and logs a clear status line.
- **Reset:** a winning settlement resets `loss_recovery_streak` to 0 (already the case) → consecutive-loss breaker auto-clears. The halt-at-cap / equity-floor breakers clear when the underlying condition recovers, or on a manual mode toggle. (Decide during impl: auto-clear vs require manual re-arm — manual is safer.)

### Hook points (from the investigation)
1. **Evaluate** the breaker in `strategy_runner._tick`, right AFTER `apply_loss_recovery_from_settlements(...)` (`strategy_runner.py:~1287-1313`), where `loss_recovery_streak` / `loss_recovery_multiplier` / equity are all known.
2. **Set** a runtime flag `self.rt.circuit_breaker_tripped` (+ reason) and record the fault.
3. **Gate** new entries on `not self.rt.circuit_breaker_tripped` in the entry-decision path — alongside the existing `_entry_limits_ok(...)` checks (`strategy_runner.py:~910`).
4. **Surface** it: a field in `/api/strategy/config`, a red banner in the dashboard strategy tab, and (optional) a row in 🐞 תקלות.

### Implementation tasks (TDD, bite-sized)
- **Task 1 — pure decision fn:** `engine/circuit_breaker.py` → `should_halt(*, streak, multiplier, cap, equity, baseline, cfg) -> Optional[str]` returning the trip reason or None. Pure, fully unit-tested (each condition + the all-off default = never trips).
- **Task 2 — config fields:** add the 5 fields to `StrategyConfig` + persistence + `/api/strategy/config` exposure. Default-off test.
- **Task 3 — wire into `_tick`:** evaluate after loss-recovery; set `self.rt.circuit_breaker_tripped`; record fault; log. Test: a synthetic loss streak trips it; a win clears it.
- **Task 4 — gate entries:** block new entries when tripped; assert open positions still TP/settle. Test.
- **Task 5 — UI:** strategy-tab toggles + a "🛑 ה-bot עצר את עצמו" banner when tripped; build:web green.
- **Task 6 — review + live verify + deploy** (default-off → safe; still a money-engine touch → explicit deploy approval required).

### Risk / rollout
- Default-off ⇒ **zero behavior change** on ship; you enable it from the dashboard when ready.
- It only ever makes the bot *safer* (stops trading) — it cannot increase risk.
- Deploying still touches the trading engine, so it needs an explicit "deploy" from you (and the push gate will ask).

---

## Part 3 — Recommended order of operations

1. **Turn the bot ON** (dashboard — it's `off` after the last deploy). This starts logging live `schema_version=1` trades, which unlocks the coach's **entry-signal lessons** (currently dormant). *(Your action; the assistant has no write auth.)*
2. **Tighten the two martingale knobs** now (Part 1): lower `loss_recovery_max_multiplier`, set a real `max_notional_per_window_usd`. Safest, highest-impact.
3. **Build + ship the circuit-breaker** (Part 2) as opt-in; enable it once you trust it.
4. **Re-read the coach lessons** after ~30+ live trades accrue; tune the TP/peak knobs against current behavior (not the historical demo data).

---

## Appendix — key code references (from the read-only investigation)
- Martingale config + sizing: `engine/strategy_runner.py:46-119` (config), `:532-539` (`_effective_investment_usd`), `:1975-2005` (entry sizing), `:910-916` (per-window notional cap), `:1287-1313` (loss-recovery apply + 80%-of-cap fault).
- Multiplier cap enforcement: `engine/loss_recovery.py:34,67` (`min(cap, …)`); incident guard: `:44-45` (SETTLE_UNKNOWN skip).
- TP / exit / peak-watchdog: `engine/strategy_runner.py:1422-1647` (TP cascade), `:1480-1524` (peak-watchdog), `:1458-1479` (hold-to-resolution).
- State counters: `engine/demo_engine.py:65-66` (`loss_recovery_streak`, `loss_recovery_multiplier`).
