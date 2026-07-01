"""
מנוע טריגרים — מסחר אוטומטי על שינויי מחיר מהירים.

שלושה מצבי טריגר:
  1. momentum  — BTC זז X% בתוך Y שניות → בדיקת מחיר חוזה → כניסה
  2. signal    — ביטחון סיגנל > סף → בדיקת מחיר חוזה → כניסה
  3. dca_pulse — כניסת DCA מהירה (N סלייסים, X שניות מרווח)

שיפורים לעומת גרסה קודמת:
  - בדיקת מחיר חוזה Polymarket (ask) לפני כל כניסה — לא רק BTC
  - בדיקת זמן שנותר בחלון — לא נכנסים בדקות האחרונות
  - זיהוי velocity של מחיר החוזה — אם החוזה כבר הגיב לתנועה, מדלגים
  - contract_ask נחשף ב-to_dict לתצוגת UI
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from atomic_io import atomic_write_text
from pricing_limits import MAX_LEGIT_SHARE_PRICE_USD

FEE_RATE = 0.002  # 0.2% — תואם ל-demo_engine

# B-5: לקוח httpx משותף עם keep-alive ללולאות ה-DCA/TP. הלולאות רצות כל 2-3s לאורך כל החלון
# ופתחו client חדש בכל איטרציה (TLS handshake). ללא result cache — get_clob_book מושך חי.
_TRIGGER_BOOK_CLIENT: Optional[httpx.AsyncClient] = None


def _get_trigger_book_client() -> httpx.AsyncClient:
    global _TRIGGER_BOOK_CLIENT
    if _TRIGGER_BOOK_CLIENT is None:
        _TRIGGER_BOOK_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _TRIGGER_BOOK_CLIENT

_DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))).resolve()
_TRIGGER_POSITIONS_PATH = _DATA_ROOT / "trigger_positions.json"

TriggerMode = Literal["momentum", "signal", "dca_pulse", "off"]

# ── כמה שניות מינימום חייבות להישאר בחלון לפני שנכנסים ──────────────────────
MIN_SECONDS_REMAINING_DEFAULT = 90  # 1.5 דקות


@dataclass
class TriggerConfig:
    mode: TriggerMode = "off"

    # ── Momentum ────────────────────────────────────────────────────────────────
    momentum_pct: float = 0.20       # % שינוי BTC מינימלי
    momentum_window_sec: int = 60    # חלון זמן לחישוב שינוי
    momentum_direction: str = "auto" # auto | Up | Down

    # ── Signal ──────────────────────────────────────────────────────────────────
    signal_confidence: float = 0.68
    signal_direction: str = "auto"

    # ── DCA Pulse ───────────────────────────────────────────────────────────────
    dca_pulse_slices: int = 3
    dca_pulse_interval_sec: float = 20.0
    dca_pulse_direction: str = "Up"   # Up | Down | auto
    # אופן חלוקת ההשקעה בין סלייסים:
    #   equal    — חלוקה שווה ($X לכל סלייס)
    #   pyramid  — יותר כסף בסלייסים מאוחרים (ממוצע עולה כשהמחיר יורד)
    #   fixed_contracts — אותה כמות חוזים לכל סלייס ($ משתנה לפי מחיר ברגע הכניסה)
    dca_sizing: str = "equal"
    # שינוי מינימלי במחיר החוזה בין סלייס לסלייס (%)
    # 0 = כבוי, 20 = הסלייס הבא חייב להיות זול ב-20% מהקודם
    dca_min_step_pct: float = 0.0

    # ── Common ──────────────────────────────────────────────────────────────────
    investment_usd: float = 5.0
    entry_price_cents: float = 30.0   # cap — לא נשלם יותר מזה לחוזה
    take_profit_pct: float = 15.0
    max_triggers_per_window: int = 2
    cooldown_sec: float = 60.0
    min_seconds_remaining: int = 90   # לא נכנסים אם נשאר פחות מזה בחלון
    contract_max_drift_pct: float = 30.0
    btc_window: str = "5m"            # 5m | 15m — חלון המסחר של Polymarket

    active: bool = False
    auto_start: bool = False  # הפעל אוטומטית כשהמנוע עולה


@dataclass
class TriggerEvent:
    ts: float
    event_type: str  # executed | skipped | error | contract_check
    trigger_mode: str
    side: Optional[str]
    price: Optional[float]      # cap ששלחנו
    contract_ask: Optional[float]  # ask בפועל ברגע הטריגר
    contracts: Optional[int]
    note: str


class TriggerEngine:
    def __init__(self) -> None:
        self.config = TriggerConfig()
        self.events: list[TriggerEvent] = []
        self.last_trigger_ts: float = 0.0
        self.triggers_this_window: int = 0
        self.current_window_epoch: int = 0
        self._task: Optional[asyncio.Task] = None
        self._price_history: list[tuple[float, float]] = []   # BTC (ts, price)
        self._contract_ask_history: dict[str, list[tuple[float, float]]] = {}  # side → [(ts, ask)]
        self._status: str = "כבוי"
        self.status_log: list[dict[str, Any]] = []   # feed חי — עד 30 שורות אחרונות
        self.current_btc_change_pct: Optional[float] = None
        self.current_signal_confidence: Optional[float] = None
        self.current_signal_rec: str = "neutral"
        self.current_contract_ask: Optional[float] = None   # ask נוכחי לצד שבוחנים
        self._demo: Any = None
        self._dca_running: bool = False
        self._last_tp_exit_check_ts: float = 0.0  # throttling לבדיקות TP בזמן dca_pulse
        self._dca_completed_epoch: int = 0  # epoch של החלון האחרון שבו DCA רץ
        self._last_dca_completed_skip_log_ts: float = 0.0  # מניעת spam בלוג על דילוג הפעלה
        # מעקב פוזיציות שנפתחו ע"י הטריגר — לצורך TP אוטומטי
        # token_id → {side, avg_cost, contracts, tp_pct, entry_ts}
        self._trigger_positions: dict[str, dict[str, Any]] = {}
        self._load_trigger_positions()  # שחזור מדיסק
        self._last_status_log_ts: float = 0.0  # לוודא שהפיד מתעדכן כל ~2s גם אם ההודעה זהה

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        now = time.time()
        same_msg = value == self._status
        # לוג כל שינוי, או אם עברו ≥2s מהכניסה האחרונה (גם אם ההודעה זהה)
        if same_msg and (now - self._last_status_log_ts) < 2.0:
            return
        self._status = value
        self._last_status_log_ts = now
        self.status_log.append({"ts": now, "msg": value})
        self.status_log = self.status_log[-50:]  # שמור 50 אחרונות

    def inject(self, demo: Any) -> None:
        self._demo = demo

    # ── Persistence for trigger_positions ─────────────────────────────────────

    def _save_trigger_positions(self) -> None:
        """שמירת פוזיציות פתוחות לדיסק — שורד restart."""
        try:
            atomic_write_text(
                _TRIGGER_POSITIONS_PATH,
                json.dumps(self._trigger_positions, indent=2),
            )
        except Exception:
            pass

    def _load_trigger_positions(self) -> None:
        """טעינת פוזיציות מדיסק."""
        try:
            if _TRIGGER_POSITIONS_PATH.exists():
                data = json.loads(_TRIGGER_POSITIONS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._trigger_positions = data
        except Exception:
            pass

    # ── Lifecycle ────────────────────────────────────────────────────────────────

    def start_loop(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    def stop_loop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.status = f"שגיאה פנימית: {str(e)[:80]}"
            await asyncio.sleep(2)

    # ── Main tick ────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        # TP exits — תמיד, גם כשכבוי/המתנה
        await self._check_tp_exits()

        if not self.config.active or self.config.mode == "off":
            if not self._trigger_positions:
                self.status = "כבוי"
            return

        await self._sync_window_epoch()

        # cooldown
        since_last = time.time() - self.last_trigger_ts
        if self.last_trigger_ts > 0 and since_last < self.config.cooldown_sec:
            remaining = int(self.config.cooldown_sec - since_last)
            self.status = f"⏱ המתנה {remaining}ש׳"
            return

        if self.config.mode == "momentum":
            await self._check_momentum()
        elif self.config.mode == "signal":
            await self._check_signal()
        elif self.config.mode == "dca_pulse":
            if not self._dca_running and self._dca_completed_epoch != self.current_window_epoch:
                await self._run_dca_pulse()
            elif self._dca_completed_epoch == self.current_window_epoch and not self._dca_running:
                # משתמש מפעיל שוב בתוך אותו epoch: בלי לוג כזה קשה לדעת "למה לא זז".
                now_ts = time.time()
                if now_ts - self._last_dca_completed_skip_log_ts >= 30:
                    self._last_dca_completed_skip_log_ts = now_ts
                    self._log_event(
                        "skipped",
                        None,
                        None,
                        None,
                        None,
                        f"DCA Pulse כבר הושלם לחלון הנוכחי (epoch={self.current_window_epoch}). "
                        "לא מתחיל מחדש עד לחלון הבא.",
                    )
                self.status = "✅ DCA Pulse הושלם (חד־פעמי) — לחץ ״הרץ שוב עכשיו״ כדי להריץ שוב"

    async def _sync_window_epoch(self) -> None:
        try:
            from market_discovery import discover_active_btc_window
            m = await discover_active_btc_window(self.config.btc_window)
            if m and m.epoch != self.current_window_epoch:
                old_epoch = self.current_window_epoch
                self.current_window_epoch = m.epoch
                self.triggers_this_window = 0
                self._contract_ask_history.clear()
                # לא מוחקים _trigger_positions — פוזיציות פתוחות ממשיכות מעקב TP
                self._dca_running = False  # איפוס מוחלט בכל חלון חדש

                # סגור פוזיציות על טוקנים מחלון קודם → פירוק לפי תוצאת החלון (SETTLE_*)
                # חשוב: בלעדי זה ה-strategy_runner לא יסגור אותן כשהוא במצב "off"
                if old_epoch != 0:
                    from market_discovery import window_step_sec

                    await self._demo.expire_all_outside_tokens(
                        (m.token_up, m.token_down),
                        context={
                            "settled_epoch": old_epoch,
                            "settled_window_sec": window_step_sec(self.config.btc_window),
                            "epoch": m.epoch,
                            "slug": getattr(m, "slug", ""),
                            "gate": "trigger:expire_rollover",
                            "reason": "מעבר חלון — Trigger Engine",
                        },
                    )
                    # נקה trigger_positions שכבר לא קיימים בדמו
                    remaining_tokens = {p.token_id for p in self._demo.state.positions}
                    expired = [t for t in self._trigger_positions if t not in remaining_tokens]
                    for t in expired:
                        self._trigger_positions.pop(t, None)
                    if expired:
                        self._save_trigger_positions()
        except Exception:
            pass

    # ── Contract price helpers ────────────────────────────────────────────────

    async def _fetch_contract_ask(self, side: str) -> Optional[float]:
        """מחזיר את ה-ask הנוכחי של חוזה Up או Down מה-CLOB של Polymarket."""
        try:
            from market_discovery import discover_active_btc_window, get_clob_book
            m = await discover_active_btc_window(self.config.btc_window)
            if not m:
                return None
            token_id = m.token_up if side == "Up" else m.token_down
            book = await get_clob_book(_get_trigger_book_client(), token_id)
            asks = book.get("asks") or []
            if not asks:
                return None
            return float(asks[0]["price"])
        except Exception:
            return None

    async def _get_window_info(self) -> Optional[tuple[int, int]]:
        """מחזיר (seconds_remaining, window_sec) לחלון הנוכחי."""
        try:
            from market_discovery import discover_active_btc_window, seconds_until_window_end
            m = await discover_active_btc_window(self.config.btc_window)
            if not m:
                return None
            return int(seconds_until_window_end(m.epoch, m.window_sec)), m.window_sec
        except Exception:
            return None

    def _track_contract_ask(self, side: str, ask: float) -> None:
        """שומר היסטוריה של ask לחוזה — לזיהוי velocity."""
        now = time.time()
        if side not in self._contract_ask_history:
            self._contract_ask_history[side] = []
        history = self._contract_ask_history[side]
        history.append((now, ask))
        # שמור רק 5 דקות אחרונות
        history[:] = [(t, p) for t, p in history if now - t <= 300]

    def _contract_drift_pct(self, side: str, ask: float) -> Optional[float]:
        """כמה % עלה מחיר החוזה מאז תחילת המעקב (first ask בחלון)."""
        history = self._contract_ask_history.get(side, [])
        if len(history) < 2:
            return None
        first_ask = history[0][1]
        if first_ask <= 0:
            return None
        return (ask - first_ask) / first_ask * 100

    # ── Pre-entry validation (contract level) ────────────────────────────────

    async def _validate_contract_entry(self, side: str) -> tuple[bool, str, Optional[float]]:
        """
        בודק שלושה תנאים לפני כניסה:
          1. זמן שנותר בחלון ≥ min_seconds_remaining
          2. מחיר ask החוזה ≤ cap (entry_price_cents / 100)
          3. החוזה לא כבר עלה יותר מ-contract_max_drift_pct מאז פתיחת החלון

        מחזיר (ok, reason, contract_ask).
        """
        cap = self.config.entry_price_cents / 100.0

        # ── 1. זמן שנותר ────────────────────────────────────────────────────────
        window_info = await self._get_window_info()
        if window_info is None:
            return False, "לא ניתן לקבל מידע חלון", None
        seconds_left, _ = window_info
        if seconds_left < self.config.min_seconds_remaining:
            reason = f"⏰ נותרו {seconds_left}ש׳ בלבד (מינימום {self.config.min_seconds_remaining}ש׳)"
            return False, reason, None

        # ── 2. מחיר ask ≤ cap ───────────────────────────────────────────────────
        ask = await self._fetch_contract_ask(side)
        self.current_contract_ask = ask
        if ask is None:
            return False, "לא ניתן לקבל ask מה-CLOB", None
        self._track_contract_ask(side, ask)

        if ask > cap:
            reason = f"ask {ask:.3f}$ > cap {cap:.3f}$ — החוזה יקר מדי"
            return False, reason, ask

        # ── 3. velocity — החוזה כבר הגיב? (0 = כבוי) ───────────────────────────
        if self.config.contract_max_drift_pct > 0:
            drift = self._contract_drift_pct(side, ask)
            if drift is not None and drift > self.config.contract_max_drift_pct:
                reason = f"⚡ מחיר החוזה כבר עלה {drift:.1f}% מאז הפתיחה — מאחרים לפוזיציה"
                return False, reason, ask

        return True, f"ask {ask:.3f}$ ≤ cap {cap:.3f}$ | {seconds_left}ש׳ בחלון", ask

    # ── Momentum mode ─────────────────────────────────────────────────────────

    async def _check_momentum(self) -> None:
        btc_price = await self._fetch_btc_price()
        if not btc_price:
            self.status = "⚠ לא ניתן לקבל מחיר BTC"
            return

        now = time.time()
        self._price_history.append((now, btc_price))
        cutoff = now - self.config.momentum_window_sec
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]

        if len(self._price_history) < 3:
            self.status = f"📊 בונה היסטוריה BTC ({len(self._price_history)} נק׳)…"
            return

        old_price = self._price_history[0][1]
        change_pct = (btc_price - old_price) / old_price * 100
        self.current_btc_change_pct = round(change_pct, 4)

        threshold = self.config.momentum_pct
        bar_filled = min(int(abs(change_pct) / max(threshold, 0.01) * 10), 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        direction_char = "▲" if change_pct >= 0 else "▼"

        # מציג גם את ask הנוכחי
        ask_str = f" | ask: {self.current_contract_ask:.3f}$" if self.current_contract_ask else ""
        self.status = f"{direction_char} BTC {change_pct:+.3f}% [{bar}] סף:{threshold:.2f}%{ask_str}"

        if abs(change_pct) < threshold:
            return

        # נקבע כיוון
        side = self.config.momentum_direction
        if side == "auto":
            side = "Up" if change_pct >= 0 else "Down"

        # ── בדיקת מחיר חוזה לפני כניסה ─────────────────────────────────────────
        ok, reason, ask = await self._validate_contract_entry(side)
        if not ok:
            self.status = f"🚀 BTC {change_pct:+.3f}% → {side} | ⛔ {reason}"
            self._log_event("skipped", side, None, ask, None,
                            f"מומנטום {change_pct:+.3f}% | {reason}")
            # reset history כדי לא לטרגר שוב מיד
            self._price_history.clear()
            return

        self.status = f"🚀 טריגר! BTC {change_pct:+.3f}% | {reason}"
        self._price_history.clear()
        await self._execute_trade(
            side, ask,
            f"מומנטום {change_pct:+.3f}% תוך {self.config.momentum_window_sec}ש׳",
        )

    # ── Signal mode ───────────────────────────────────────────────────────────

    async def _check_signal(self) -> None:
        try:
            from signal_engine import compute_signals
            result = await compute_signals()
        except Exception as e:
            self.status = f"⚠ שגיאת סיגנל: {str(e)[:60]}"
            return

        rec = result.get("recommendation", "neutral")
        up_conf = result.get("up_confidence", 0.5)
        down_conf = result.get("down_confidence", 0.5)
        self.current_signal_rec = rec
        self.current_signal_confidence = round(
            up_conf if rec == "Up" else down_conf if rec == "Down" else max(up_conf, down_conf),
            4,
        )

        conf_pct = round(self.current_signal_confidence * 100, 1)
        threshold_pct = round(self.config.signal_confidence * 100, 1)

        ask_str = f" | ask: {self.current_contract_ask:.3f}$" if self.current_contract_ask else ""
        self.status = f"📡 {rec} {conf_pct}% (סף:{threshold_pct}%){ask_str}"

        if rec == "neutral" or self.current_signal_confidence < self.config.signal_confidence:
            return

        side = rec if self.config.signal_direction == "auto" else self.config.signal_direction

        ok, reason, ask = await self._validate_contract_entry(side)
        if not ok:
            self.status = f"📡 {rec} {conf_pct}% | ⛔ {reason}"
            self._log_event("skipped", side, None, ask, None,
                            f"סיגנל {rec} {conf_pct}% | {reason}")
            return

        self.status = f"📡 טריגר! {rec} {conf_pct}% | {reason}"
        await self._execute_trade(side, ask, f"סיגנל {rec} ביטחון {conf_pct}%")

    # ── DCA Pulse mode ────────────────────────────────────────────────────────

    async def _wait_for_price(self, side: str, slice_num: int, total: int, effective_cap: Optional[float] = None) -> tuple[bool, Optional[float]]:
        """
        ממתין עד שה-ask יורד לתחת הcap — בודק כל 2 שניות.
        מחזיר (ok, ask) כשהמחיר מגיע, או (False, None) אם נגמר הזמן בחלון.
        effective_cap אופציונלי — מחיר מקסימלי לסלייס זה (כולל dca_min_step_pct).
        """
        cap = effective_cap if effective_cap is not None else (self.config.entry_price_cents / 100.0)
        cap_cents = round(cap * 100, 1)
        while True:
            # בדוק זמן שנותר
            window_info = await self._get_window_info()
            if window_info is None:
                self.status = f"⛔ DCA {side} סלייס {slice_num}/{total} | לא ניתן לקבל מידע חלון"
                return False, None
            seconds_left, _ = window_info
            if seconds_left < self.config.min_seconds_remaining:
                self.status = f"⏰ DCA {side} סלייס {slice_num}/{total} | פג הזמן — {seconds_left}ש׳ < {self.config.min_seconds_remaining}ש׳"
                self._log_event("skipped", side, None, None, None,
                                f"DCA Pulse סלייס {slice_num}/{total} | ⏰ פג הזמן ({seconds_left}ש׳)")
                return False, None

            # בדוק מחיר
            # אם בזמן dca הפוזיציות כבר מספיק רווחיות — TP צריך להיסגר בזמן אמת.
            now_ts = time.time()
            if now_ts - self._last_tp_exit_check_ts >= 2.0:
                self._last_tp_exit_check_ts = now_ts
                await self._check_tp_exits()

            ask = await self._fetch_contract_ask(side)
            self.current_contract_ask = ask
            if ask is None:
                self.status = f"⏳ DCA {side} סלייס {slice_num}/{total} | ממתין למחיר... ({seconds_left}ש׳)"
                await asyncio.sleep(2)
                continue

            self._track_contract_ask(side, ask)
            ask_cents = round(ask * 100, 1)

            if ask <= cap:
                return True, ask

            # מחיר גבוה מדי — ממתין
            self.status = (
                f"⏳ DCA {side} סלייס {slice_num}/{total} | "
                f"מחיר: {ask_cents}¢ → ממתין ל-{cap_cents}¢ | {seconds_left}ש׳"
            )
            await asyncio.sleep(2)

    async def _resolve_dca_side(self, slice_num: int, total: int) -> Optional[str]:
        """
        כשdca_pulse_direction=auto — שואל את מנוע הסיגנלים ומחזיר Up/Down.
        אם הסיגנל ניטרלי — ממתין עד שיהיה כיוון ברור (עד סוף הזמן).
        """
        from market_discovery import discover_active_btc_window, get_clob_book, seconds_until_window_end
        import httpx as _httpx
        from signal_engine import compute_signals

        while True:
            # בדוק זמן
            try:
                m = await discover_active_btc_window(self.config.btc_window)
                if m and seconds_until_window_end(m.epoch, m.window_sec) < self.config.min_seconds_remaining:
                    self.status = f"⏰ DCA אוטו סלייס {slice_num}/{total} | פג הזמן"
                    return None
            except Exception:
                pass

            # שאל סיגנל
            try:
                client = _get_trigger_book_client()
                up_book = await get_clob_book(client, m.token_up) if m else None
                down_book = await get_clob_book(client, m.token_down) if m else None
                result = await compute_signals(up_book=up_book, down_book=down_book,
                                               window_sec=m.window_sec if m else 300)
                rec = result.get("recommendation", "neutral")
                conf = result.get("confidence_pct", 0)
                if rec in ("Up", "Down"):
                    self.status = f"🤖 DCA אוטו סלייס {slice_num}/{total} | סיגנל: {rec} {conf}%"
                    return rec
                else:
                    self.status = f"🤖 DCA אוטו סלייס {slice_num}/{total} | ניטרלי {conf}% — ממתין לכיוון..."
            except Exception:
                self.status = f"🤖 DCA אוטו סלייס {slice_num}/{total} | שגיאת סיגנל — מנסה שוב..."

            await asyncio.sleep(3)

    async def _auto_wait_for_best_price(
        self, slice_num: int, total: int, effective_cap: float
    ) -> tuple[Optional[str], Optional[float]]:
        """
        מצב אוטו: בכל טיק — שואל סיגנל, בודק מחיר שני הצדדים.
        מחזיר (side, ask) לצד הראשון שמחירו ≤ effective_cap.
        מעדיף את הצד שהסיגנל ממליץ עליו, אבל אם יקר — עובר לצד השני.
        מחזיר (None, None) אם פג הזמן.
        """
        from market_discovery import discover_active_btc_window, get_clob_book, seconds_until_window_end
        import httpx as _httpx
        from signal_engine import compute_signals

        cap_cents = round(effective_cap * 100, 1)

        while True:
            # בדוק זמן
            try:
                m = await discover_active_btc_window(self.config.btc_window)
            except Exception:
                m = None
            if m is None:
                self.status = f"⏳ DCA אוטו סלייס {slice_num}/{total} | ממתין לחלון..."
                await asyncio.sleep(2)
                continue
            seconds_left = int(seconds_until_window_end(m.epoch, m.window_sec))
            if seconds_left < self.config.min_seconds_remaining:
                self.status = f"⏰ DCA אוטו סלייס {slice_num}/{total} | פג הזמן"
                return None, None

            # שאל סיגנל וקבל מחירי שני הצדדים במקביל
            # חשוב: בזמן dca_pulse צריך גם להפעיל TP בזמן אמת, אחרת ה-UI יכול להראות שיאים גדולים לפני יציאה.
            now_ts = time.time()
            if now_ts - self._last_tp_exit_check_ts >= 2.0:
                self._last_tp_exit_check_ts = now_ts
                await self._check_tp_exits()

            try:
                client = _get_trigger_book_client()
                up_book, down_book = await asyncio.gather(
                    get_clob_book(client, m.token_up),
                    get_clob_book(client, m.token_down),
                )
                up_ask = float(up_book["asks"][0]["price"]) if up_book.get("asks") else None
                down_ask = float(down_book["asks"][0]["price"]) if down_book.get("asks") else None
                result = await compute_signals(up_book=up_book, down_book=down_book,
                                               window_sec=m.window_sec)
                rec = result.get("recommendation", "neutral")
                conf = result.get("confidence_pct", 0)
                up_conf = float(result.get("up_confidence", 0.5))
                down_conf = float(result.get("down_confidence", 0.5))
            except Exception:
                await asyncio.sleep(2)
                continue

            # חשוב: בצדדים אמיתיים מותר רק Up/Down.
            # אם הסיגנל ניטרלי (recommendation="neutral") — עדיין אפשר להיכנס לפי "מחיר זמין מתחת ל-cap".
            if rec == "neutral":
                up_ok = up_ask is not None and up_ask <= effective_cap
                down_ok = down_ask is not None and down_ask <= effective_cap

                if up_ok or down_ok:
                    # בחירה לפי מחיר (ואם שווה — תיעדוף קונפידנס גבוה יותר).
                    choose_up: bool
                    if up_ok and down_ok:
                        if up_ask < down_ask:
                            choose_up = True
                        elif down_ask < up_ask:
                            choose_up = False
                        else:
                            choose_up = up_conf >= down_conf
                    else:
                        choose_up = bool(up_ok)

                    chosen_side = "Up" if choose_up else "Down"
                    chosen_ask = up_ask if choose_up else down_ask
                    chosen_cents = round((chosen_ask or 0) * 100, 1)
                    self.status = (
                        f"🤖 DCA אוטו | ניטרלי {conf}% — בחרתי {chosen_side} "
                        f"{chosen_cents}¢ ≤ {cap_cents}¢ ✅"
                    )
                    return chosen_side, chosen_ask

                # שני הצדדים מעל ה-cap — ממתין.
                self.status = (
                    f"⏳ DCA אוטו סלייס {slice_num}/{total} | ניטרלי {conf}% | "
                    f"Up {up_ask and round(up_ask*100,1) or '?'}¢ · Down {down_ask and round(down_ask*100,1) or '?'}¢ "
                    f"→ ממתין ל-{cap_cents}¢ | {seconds_left}ש׳"
                )
                await asyncio.sleep(2)
                continue

            other = "Down" if rec == "Up" else "Up"
            rec_ask = up_ask if rec == "Up" else down_ask
            other_ask = down_ask if rec == "Up" else up_ask

            rec_cents = round((rec_ask or 0) * 100, 1)
            other_cents = round((other_ask or 0) * 100, 1)

            # בדוק הצד המועדף
            if rec_ask is not None and rec_ask <= effective_cap:
                self.status = f"🤖 DCA אוטו | סיגנל: {rec} {conf}% | {rec_cents}¢ ≤ {cap_cents}¢ ✅"
                return rec, rec_ask

            # הצד המועדף יקר — בדוק הצד הנגדי
            if other_ask is not None and other_ask <= effective_cap:
                self.status = (
                    f"🤖 DCA אוטו | {rec} {rec_cents}¢ יקר — נכנס ב-{other} {other_cents}¢ ≤ {cap_cents}¢ ✅"
                )
                return other, other_ask

            # שני הצדדים יקרים — ממתין
            self.status = (
                f"⏳ DCA אוטו סלייס {slice_num}/{total} | "
                f"Up {up_ask and round(up_ask*100,1) or '?'}¢ · Down {down_ask and round(down_ask*100,1) or '?'}¢ "
                f"→ ממתין ל-{cap_cents}¢ | {seconds_left}ש׳"
            )
            await asyncio.sleep(2)

    def _dca_slice_amounts(self) -> list[float]:
        """
        מחשב כמה $ להשקיע בכל סלייס לפי dca_sizing:
          equal           — חלוקה שווה
          pyramid         — יותר כסף בסלייסים מאוחרים (1:2:3:...:N)
          fixed_contracts — מחזיר None לכל סלייס (כמות חוזים נקבעת לפי מחיר בזמן אמת)
        """
        total = self.config.dca_pulse_slices
        inv = self.config.investment_usd
        sizing = self.config.dca_sizing

        if sizing == "pyramid":
            # משקולות 1, 2, 3, ..., N — יותר כסף בסלייסים מאוחרים
            weights = list(range(1, total + 1))
            total_w = sum(weights)
            return [inv * w / total_w for w in weights]
        else:  # equal / fixed_contracts
            return [inv / total] * total

    async def _run_dca_pulse(self) -> None:
        self._dca_running = True
        cfg_side = self.config.dca_pulse_direction
        total = self.config.dca_pulse_slices
        slice_amounts = self._dca_slice_amounts()
        cap = self.config.entry_price_cents / 100.0
        last_entry_ask: Optional[float] = None  # מחיר הכניסה בסלייס הקודם
        executed_any = False  # חשוב: לא "ננעל" על epoch אם לא בוצעה אפילו כניסה אחת

        try:
            for i in range(total):
                slice_usd = slice_amounts[i]

                # חשב effective_cap לפי dca_min_step_pct
                if last_entry_ask is not None and self.config.dca_min_step_pct > 0:
                    step_cap = last_entry_ask * (1.0 - self.config.dca_min_step_pct / 100.0)
                    effective_cap = min(cap, step_cap)
                else:
                    effective_cap = cap

                if cfg_side == "auto":
                    # מצב אוטו: מחפש את הצד הזול ביותר שמתחת לcap, עם עדיפות לסיגנל
                    side, ask = await self._auto_wait_for_best_price(i + 1, total, effective_cap)
                    if side not in ("Up", "Down"):  # None = timeout, או כל ערך אחר שאינו חוקי
                        break
                    ok = True
                else:
                    side = cfg_side
                    effective_cap_cents = round(effective_cap * 100, 1)
                    if self.config.dca_sizing == "fixed_contracts":
                        fixed_contracts = max(1, int(self.config.investment_usd / cap / total))
                        self.status = (
                            f"🔄 DCA Pulse {side}: סלייס {i+1}/{total} | "
                            f"{fixed_contracts} חוזים קבועים — בודק מחיר ≤{effective_cap_cents}¢..."
                        )
                    else:
                        self.status = (
                            f"🔄 DCA Pulse {side}: סלייס {i+1}/{total} | "
                            f"${slice_usd:.2f} — בודק מחיר ≤{effective_cap_cents}¢..."
                        )
                    ok, ask = await self._wait_for_price(side, i + 1, total, effective_cap=effective_cap)
                    if not ok:
                        break

                last_entry_ask = ask

                if self.config.dca_sizing == "fixed_contracts":
                    fixed_contracts = max(1, int(self.config.investment_usd / cap / total))
                    ok_exec = await self._execute_trade(
                        side, ask,
                        f"DCA Pulse סלייס {i+1}/{total} | {fixed_contracts} חוזים קבועים",
                        contracts_override=fixed_contracts,
                    )
                else:
                    ok_exec = await self._execute_trade(
                        side, ask,
                        f"DCA Pulse סלייס {i+1}/{total}",
                        amount_override=slice_usd,
                    )
                executed_any = executed_any or bool(ok_exec)

                if i < total - 1:
                    for remaining in range(int(self.config.dca_pulse_interval_sec), 0, -1):
                        self.status = f"🔄 DCA Pulse: {i+1}/{total} ✓ | הבא בעוד {remaining}ש׳"
                        await asyncio.sleep(1)
            # בהתאם ל-UI: DCA Pulse הוא חד-פעמי — לא מתחיל שוב אוטומטית,
            # אבל גם לא "מכבה" את הטריגר (כדי לא לבלבל: עדיין מנטר TP לפוזיציות פתוחות).
            if executed_any:
                self.status = f"✅ DCA Pulse הסתיים ({total} סלייסים) | חד־פעמי (מוכן להרצה מחדש)"
                self._log_event(
                    "executed",
                    None,
                    None,
                    None,
                    None,
                    f"DCA Pulse הסתיים ({total} סלייסים) — חד־פעמי (לא ירוץ שוב עד rearm)",
                )
            else:
                self.status = f"⏭ DCA Pulse הסתיים בלי כניסות | חד־פעמי (מוכן להרצה מחדש)"
                self._log_event(
                    "skipped",
                    None,
                    None,
                    None,
                    None,
                    f"DCA Pulse הסתיים בלי כניסות ({total} סלייסים) — חד־פעמי (לא ירוץ שוב עד rearm)",
                )
        finally:
            self._dca_running = False
            # אם לא בוצעה אף כניסה בפועל — אל "תנעל" את החלון, כדי שהמנוע יוכל לנסות שוב
            # (אחרת המשתמש רואה trigger_skipped אינסופי בלי אף טרייד).
            if executed_any:
                self._dca_completed_epoch = self.current_window_epoch
            else:
                self._dca_completed_epoch = 0
            self.last_trigger_ts = time.time()

    # ── Trade execution ───────────────────────────────────────────────────────

    async def _execute_trade(
        self,
        side: str,
        contract_ask: Optional[float],
        note: str,
        amount_override: Optional[float] = None,
        contracts_override: Optional[int] = None,
    ) -> bool:
        if self._demo is None:
            self._log_event("error", side, None, contract_ask, None, "demo engine לא מחובר")
            return False

        if self.triggers_this_window >= self.config.max_triggers_per_window:
            self.status = f"🛑 מקסימום טריגרים ({self.config.max_triggers_per_window}) לחלון"
            self._log_event("skipped", side, None, contract_ask, None, "מקסימום טריגרים לחלון")
            return False

        try:
            from market_discovery import discover_active_btc_window, seconds_until_window_end
            m = await discover_active_btc_window(self.config.btc_window)
            if not m:
                self._log_event("error", side, None, contract_ask, None, "לא נמצא שוק פעיל")
                return False
        except Exception as e:
            self._log_event("error", side, None, contract_ask, None, f"שגיאת גילוי שוק: {e}")
            return False

        cap = self.config.entry_price_cents / 100.0
        amount = amount_override if amount_override is not None else self.config.investment_usd
        oms = int(m.order_min_size)
        if contracts_override is not None:
            # מצב fixed_contracts (או בקרה מפורשת) — כמות חוזים קבועה, לא לחשב מ־cap
            contracts = max(oms, int(contracts_override))
        else:
            # מילוי בפועל ≤ cap: כדי לנצל ~$amount, מחשבים לפי מחיר המילוי (ולא לפי cap),
            # אחרת כש ask מחוץ ל-cap מקבלים הרבה פחות דולרים מהמיועד.
            fill_ref = min(float(contract_ask), cap) if contract_ask is not None else cap
            if fill_ref <= 0 or fill_ref > cap * 1.1:
                fill_ref = cap
            unit_cost = fill_ref * (1.0 + FEE_RATE)
            contracts = int(amount / unit_cost) if unit_cost > 0 else 0
            contracts = max(oms, contracts)
        token_id = m.token_up if side == "Up" else m.token_down

        # context מלא — ממלא את עמודות Gate / סיבה / epoch בטבלת העסקאות
        seconds_left = int(seconds_until_window_end(m.epoch, m.window_sec))
        trade_context = {
            "order_min_size": float(m.order_min_size),
            "epoch": m.epoch,
            "slug": getattr(m, "slug", None) or getattr(m, "question", ""),
            "window_sec": m.window_sec,
            "gate": f"trigger:{self.config.mode}",
            "min_left_sec": seconds_left,
            "reason": note,
        }

        try:
            result = await self._demo.simulate_market_buy(
                side, token_id, float(contracts), cap,
                context=trade_context,
            )
            if result.get("ok"):
                trade_data = result.get("trade") or {}
                fill_price = trade_data.get("price") or cap
                contracts = int(trade_data.get("contracts", contracts))  # כמות בפועל
                total_cost = fill_price * contracts

                # עדכן/פתח פוזיציה לפני בניית ה-note — כך ה-note משקף את הפוזיציה הצטברית
                if token_id in self._trigger_positions:
                    existing = self._trigger_positions[token_id]
                    old_c = existing["contracts"]
                    new_c = old_c + contracts
                    existing["avg_cost"] = (existing["avg_cost"] * old_c + fill_price * contracts) / new_c if new_c else fill_price
                    existing["contracts"] = new_c
                    existing["tp_pct"] = self.config.take_profit_pct
                else:
                    self._trigger_positions[token_id] = {
                        "side": side,
                        "avg_cost": fill_price,
                        "contracts": contracts,
                        "tp_pct": self.config.take_profit_pct,
                        "entry_ts": time.time(),
                    }
                self._save_trigger_positions()

                pos = self._trigger_positions[token_id]
                combined_avg = pos["avg_cost"]
                combined_contracts = pos["contracts"]
                combined_cost = combined_avg * combined_contracts
                tp_target_raw = combined_avg * (1.0 + self.config.take_profit_pct / 100.0)
                tp_target_price = min(tp_target_raw, MAX_LEGIT_SHARE_PRICE_USD)
                tp_target_profit = combined_cost * (self.config.take_profit_pct / 100.0)
                cap_note = (
                    " | יעד TP מוגבל לתקרת החוזה (≤99¢)"
                    if tp_target_price < tp_target_raw - 1e-12
                    else ""
                )

                slice_note = (
                    f"| fill: {fill_price:.3f}$ | עלות סלייס: {total_cost:.2f}$ "
                    f"| 📊 פוזיציה: {combined_contracts} חוזים @ avg {combined_avg*100:.1f}¢ "
                    f"| TP @ {tp_target_price*100:.1f}¢ (≈+{tp_target_profit:.2f}$){cap_note}"
                )
                self._log_event("executed", side, cap, contract_ask, contracts,
                                f"{note} {slice_note}")
                self.last_trigger_ts = time.time()
                self.triggers_this_window += 1
                ask_str = f" (ask היה {contract_ask:.3f}$)" if contract_ask else ""
                self.status = f"✅ {side} {contracts} חוזים @ cap {cap:.2f}${ask_str}"
                return True
            else:
                err = result.get("error", "כשל לא ידוע")
                self._log_event("error", side, cap, contract_ask, contracts, err)
                self.status = f"⚠ כשל: {err}"
                return False
        except Exception as e:
            self._log_event("error", side, cap, contract_ask, None, str(e))
            self.status = f"⚠ שגיאה: {str(e)[:60]}"
            return False

    # ── TP Exit monitoring ────────────────────────────────────────────────────

    async def _check_tp_exits(self) -> None:
        """
        בודק כל פוזיציה פתוחה שנכנסה דרך הטריגר.
        אם ה-bid הנוכחי הגיע לסף ה-TP — מוכר ומתעד.
        נקרא בכל tick, גם כשאין כניסות חדשות.
        """
        if not self._trigger_positions or self._demo is None:
            return

        try:
            from market_discovery import get_clob_book
            import httpx as _httpx
        except Exception:
            return

        exited: list[str] = []
        # B-5: לקוח משותף keep-alive (nullcontext כדי לא לסגור אותו ולא לשנות הזחה בלולאה).
        async with contextlib.nullcontext(_get_trigger_book_client()) as client:
            for token_id, pos in list(self._trigger_positions.items()):
                try:
                    # בדוק שהפוזיציה עדיין קיימת ב-demo engine (מקור אמת)
                    demo_idx = self._demo._position_idx(token_id)
                    if demo_idx < 0:
                        # הפוזיציה כבר נמכרה/נעלמה — ננקה מעקב
                        exited.append(token_id)
                        continue

                    demo_pos = self._demo.state.positions[demo_idx]
                    avg_cost = demo_pos.avg_cost     # ממוצע משוקלל אמיתי
                    contracts = int(demo_pos.contracts)
                    side = pos["side"]
                    tp_pct = pos["tp_pct"]

                    book = await get_clob_book(client, token_id)
                    bids = book.get("bids") or []
                    if not bids:
                        continue
                    bid = float(bids[0]["price"])

                    upnl_pct = (bid - avg_cost) / avg_cost * 100.0 if avg_cost > 0 else 0.0

                    # עדכן סטטוס — הצג כמה רחוק מה-TP (מחיר יעד מוגבל לתקרת חוזה חוקית)
                    tp_target_raw = avg_cost * (1 + tp_pct / 100.0)
                    tp_eff = min(tp_target_raw, MAX_LEGIT_SHARE_PRICE_USD)
                    bid_cents = round(bid * 100, 1)
                    tp_cents = round(tp_eff * 100, 1)
                    avg_cents = round(avg_cost * 100, 1)
                    cost_total = avg_cost * contracts
                    cap_hint = " (מוגבל ל-99¢)" if tp_eff < tp_target_raw - 1e-12 else ""
                    self.status = (
                        f"👁 {side} {contracts}x @ avg {avg_cents}¢ (={cost_total:.2f}$) | "
                        f"bid: {bid_cents}¢ | TP: {tp_cents}¢{cap_hint} ({upnl_pct:+.1f}%)"
                    )

                    if bid >= tp_eff - 1e-9:
                        # הגענו ל-TP — מוכרים
                        sell_ctx: dict[str, Any] = {}
                        try:
                            from market_discovery import discover_active_btc_window
                            m_sell = await discover_active_btc_window(self.config.btc_window)
                            if m_sell:
                                sell_ctx = {"epoch": m_sell.epoch, "window_sec": m_sell.window_sec}
                        except Exception:
                            pass
                        result = await self._demo.simulate_sell_all(token_id, context=sell_ctx)
                        if result.get("ok"):
                            proceeds = bid * contracts * (1 - FEE_RATE)
                            cost = avg_cost * contracts * (1 + FEE_RATE)
                            realized = proceeds - cost
                            self._log_event(
                                "executed", side, None, bid, contracts,
                                f"🎯 TP +{upnl_pct:.1f}% | מכירה {contracts}x @ {bid_cents}¢ | "
                                f"avg: {avg_cents}¢ | רווח: {realized:+.2f}$",
                            )
                            self.status = f"🎯 TP! {side} {contracts}x @ {bid_cents}¢ | {realized:+.2f}$"
                            exited.append(token_id)
                        else:
                            err = result.get("error", "כשל מכירה")
                            self.status = f"⚠ TP נכשל: {err}"
                except Exception:
                    continue

        if exited:
            for tid in exited:
                self._trigger_positions.pop(tid, None)
            self._save_trigger_positions()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _fetch_btc_price(self) -> Optional[float]:
        # ההחלטות רצות על מחיר Chainlink (המקור שלפיו Polymarket סוגר); Binance רק כ-fallback.
        try:
            from btc_price import fetch_btc_current_usd
            price, _source = await fetch_btc_current_usd()
            return price
        except Exception:
            return None

    def _log_event(
        self,
        event_type: str,
        side: Optional[str],
        price: Optional[float],
        contract_ask: Optional[float],
        contracts: Optional[int],
        note: str,
    ) -> None:
        now = time.time()
        ev = TriggerEvent(
            ts=now,
            event_type=event_type,
            trigger_mode=self.config.mode,
            side=side,
            price=price,
            contract_ask=contract_ask,
            contracts=contracts,
            note=note,
        )
        self.events.append(ev)
        self.events = self.events[-50:]

        # לוג v2 — נכתב לאותו run_dir כמו שאר הלוגים
        try:
            from run_logging import append_event as _append_event
            _append_event(f"trigger_{event_type}", {
                "trigger_mode": self.config.mode,
                "side": side,
                "cap_price": price,
                "contract_ask": contract_ask,
                "contracts": contracts,
                "note": note,
                "btc_window": self.config.btc_window,
                "investment_usd": self.config.investment_usd,
                "take_profit_pct": self.config.take_profit_pct,
            })
        except Exception:
            pass

    # ── API ───────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        cooldown_remaining: Optional[float] = None
        if self.last_trigger_ts > 0:
            remaining = self.config.cooldown_sec - (time.time() - self.last_trigger_ts)
            cooldown_remaining = round(max(remaining, 0.0), 1)

        return {
            "active": self.config.active,
            "mode": self.config.mode,
            "status": self.status,
            "status_log": list(self.status_log),
            "current_window_epoch": self.current_window_epoch,
            "dca_running": self._dca_running,
            "dca_completed_epoch": self._dca_completed_epoch,
            "last_trigger_ts": self.last_trigger_ts,
            "triggers_this_window": self.triggers_this_window,
            "cooldown_remaining": cooldown_remaining,
            "current_btc_change_pct": self.current_btc_change_pct,
            "current_signal_confidence": self.current_signal_confidence,
            "current_signal_rec": self.current_signal_rec,
            "current_contract_ask": self.current_contract_ask,
            "events": [asdict(e) for e in self.events[-15:]],
            "config": asdict(self.config),
            "open_positions": {
                tid: {
                    "side": pos["side"],
                    "avg_cost": pos["avg_cost"],
                    "contracts": pos["contracts"],
                    "tp_pct": pos["tp_pct"],
                    "entry_ts": pos["entry_ts"],
                }
                for tid, pos in self._trigger_positions.items()
            },
        }
