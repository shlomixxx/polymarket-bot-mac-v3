"""
לולאת אסטרטגיה: זמן בחלון, DCA, גידור, TP נטו, חצי/מלא אוטומטי.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

import httpx

import live_clob
from demo_engine import DemoEngine, FEE_RATE
from market_discovery import discover_active_btc_window, get_clob_book, seconds_until_window_end
from order_validation import validate_contracts_for_market
from pricing_limits import MAX_LEGIT_SHARE_PRICE_USD, MIN_LEGIT_SHARE_PRICE_USD

Mode = Literal["off", "semi", "auto"]


def _fmt_px(x: Optional[float]) -> str:
    """מחיר לשורת סטטוס — בלי nan/inf."""
    if x is None:
        return "—"
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(xf):
        return "—"
    return f"{xf:.2f}"


BtcMarketWindow = Literal["5m", "15m"]


@dataclass
class StrategyConfig:
    investment_usd: float = 5.0
    entry_price_cents: float = 20.0
    min_contracts: int = 5
    # שוק BTC Up/Down: חלון 5 דק׳ או 15 דק׳ (slug שונה ב-Polymarket)
    btc_window: BtcMarketWindow = "5m"
    take_profit_pct: float = 20.0
    min_minutes_for_entry: float = 3.0
    freeze_last_minutes: float = 1.0
    intermediate_block_new_entries: bool = True
    dca_enabled: bool = False
    dca_slices: int = 4
    dca_interval_sec: float = 30.0
    # DCA לפי זמן (ברירת מחדל) + אופציה להנחה קבועה באחוזים לכל סל
    dca_discount_enabled: bool = False
    dca_discount_pct: float = 2.0
    hedge_enabled: bool = False
    hedge_combined_ask_max: float = 0.98
    side_preference: Literal["Up", "Down", "signal"] = "Up"
    # Auto-profit controls (TP בלבד, ללא SL)
    auto_reenter_after_tp: bool = True
    reenter_cooldown_sec: float = 8.0
    max_entries_per_window: int = 3
    # Risk limits (רך) — בלי SL, רק תקרות כדי למנוע ריצה לא מבוקרת
    max_notional_per_window_usd: float = 1_000_000.0
    max_trades_per_hour: int = 1_000
    # Proximity status thresholds (for UI feedback)
    near_entry_pct: float = 3.0  # נחשב "קרוב" אם ה-Ask עד X% מעל יעד הכניסה
    near_tp_pct: float = 2.0  # נחשב "קרוב" אם חסר עד X% ל-TP
    dca_tp_override_pct: float = 50.0  # רווח לא ממומש ≥ X% מאפשר TP גם כש-DCA לא הושלם
    # 0 = כבוי; >0 = שורת יומן כל X שניות עם Ask/Bid ל־Up/Down מה־CLOB (למעקב אחרי השוק)
    book_log_interval_sec: float = 0.0
    mode: Mode = "off"
    # שחזור אחרי הפסד: הגדלת investment_usd היעד (מכפיל) עד רווח / פירוק מנצח
    loss_recovery_enabled: bool = False
    loss_recovery_step_pct: float = 20.0
    loss_recovery_every_n_losses: int = 1
    loss_recovery_max_multiplier: float = 10.0


@dataclass
class StrategyRuntime:
    config: StrategyConfig = field(default_factory=StrategyConfig)
    mode: Mode = "off"
    current_epoch: int = 0
    dca_done_slices: int = 0
    last_dca_ts: float = 0.0
    # במחיר בפועל של ה־BUY האחרון (ב-DCA) כדי שאפשר יהיה לקבוע next-limit קשיח
    # כך שכל כניסה תהיה נמוכה לפחות ב־X% מהכניסה הקודמת בפועל.
    dca_last_fill_price: Optional[float] = None
    hedge_leg2_done: bool = False
    pending_approval: Optional[dict] = None
    last_tp_ts: float = 0.0
    last_tp_side: Optional[str] = None
    tp_happened_this_window: bool = False
    entries_this_window: int = 0
    trade_timestamps: list[float] = field(default_factory=list)
    notional_this_window: float = 0.0
    log_lines: list[str] = field(default_factory=list)
    log_entries: list[dict] = field(default_factory=list)  # [{ts, msg, type, session_id?}]
    log_listeners: list[Callable[[str], None]] = field(default_factory=list)
    last_status: str = ""
    _last_status_ts: float = 0.0
    _last_status_key: str = ""
    last_tick_ts: float = 0.0
    _last_book_log_ts: float = 0.0
    # לשידור/סטטיסטיקה: מתחילים למדוד זמן מהכניסה הראשונה של לולאת האסטרטגיה (לא טריגר/גידור רגל 2).
    strategy_first_buy_ts: Optional[float] = None

    def log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-300:]
        self.log_entries.append({
            "ts": time.time(),
            "msg": msg,
            "type": "system",
        })
        self.log_entries = self.log_entries[-500:]
        for cb in self.log_listeners:
            try:
                cb(line)
            except Exception:
                pass

    def log_event(self, msg: str, session_id: Optional[str] = None) -> None:
        """אירוע בפועל: כניסה, יציאה, DCA — לא סטטוס."""
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] ▶ אירוע: {msg}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-300:]
        self.log_entries.append({
            "ts": time.time(),
            "msg": msg,
            "type": "event",
            "session_id": session_id,
        })
        self.log_entries = self.log_entries[-500:]
        for cb in self.log_listeners:
            try:
                cb(line)
            except Exception:
                pass

    def record_tp(self, *, cfg: "StrategyConfig", side: str) -> None:
        """עדכון מצב אחרי TP.
        במצב גידור אנחנו רוצים לאפשר 'רה-כניסה' של הרגל החסרה אחרי שסגרנו צד אחד.
        """
        self.tp_happened_this_window = True
        self.last_tp_ts = time.time()
        self.last_tp_side = side
        if cfg.hedge_enabled:
            # אם TP סגר צד אחד, נסמן מחדש שניתן להשלים את הרגל החסרה.
            self.hedge_leg2_done = False
        # DCA: אם TP סגר sell_all את הפוזיציה, מותר להתחיל מחדש DCA
        # כדי שלא ניתקע עם dca_done_slices>0 אבל בלי פוזיציה פתוחה.
        if cfg.dca_enabled and cfg.auto_reenter_after_tp:
            self.dca_done_slices = 0
            self.last_dca_ts = 0.0
            self.dca_last_fill_price = None

    def sync_after_demo_positions_cleared(self) -> None:
        """אחרי `/api/demo/reset` או ניקוי סטטיסטיקה עם flatten —
        הפוזיציות בדמו מתאפסות אבל מוני המנוע (DCA, TP, מגבלות) נשארו בזיכרון.
        חייבים לאפס אותם כדי שלא יופיע 'TP נעול עד DCA' בלי פוזיציה."""
        self.dca_done_slices = 0
        self.last_dca_ts = 0.0
        self.dca_last_fill_price = None
        self.hedge_leg2_done = False
        self.pending_approval = None
        self.entries_this_window = 0
        self.notional_this_window = 0.0
        self.trade_timestamps.clear()
        self.tp_happened_this_window = False
        self.last_tp_ts = 0.0
        self.last_tp_side = None
        self._last_book_log_ts = 0.0
        self.strategy_first_buy_ts = None

    def status(
        self,
        msg: str,
        *,
        key: str = "",
        session_id: Optional[str] = None,
        min_interval_sec: float = 1.0,
        repeat_interval_sec: float = 5.0,
    ) -> None:
        """סטטוס קצר: המסך מתעדכן תמיד; היומן לא מוצף בהודעות זהות."""
        now = time.time()
        k = key or msg
        prev_key = self._last_status_key
        self.last_status = msg
        if k != prev_key:
            self._last_status_key = k
            self._last_status_ts = now
            self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            self.log_lines = self.log_lines[-300:]
            self.log_entries.append({
                "ts": now,
                "msg": msg,
                "type": "status",
                "session_id": session_id,
            })
            self.log_entries = self.log_entries[-500:]
            for cb in self.log_listeners:
                try:
                    cb(self.log_lines[-1])
                except Exception:
                    pass
            return
        if now - self._last_status_ts >= max(min_interval_sec, repeat_interval_sec):
            self._last_status_ts = now
            self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            self.log_lines = self.log_lines[-300:]
            self.log_entries.append({
                "ts": now,
                "msg": msg,
                "type": "status",
                "session_id": session_id,
            })
            self.log_entries = self.log_entries[-500:]
            for cb in self.log_listeners:
                try:
                    cb(self.log_lines[-1])
                except Exception:
                    pass


def contracts_from_investment(inv: float, price_usd: float, minimum: int) -> int:
    """כמות חוזים מסכום — אם המחיר מחוץ לטווח 0.01–0.99, מחזירים 0."""
    if price_usd <= 0 or price_usd < MIN_LEGIT_SHARE_PRICE_USD or price_usd > MAX_LEGIT_SHARE_PRICE_USD:
        return 0
    n = int(inv // price_usd)
    return n if n >= minimum else 0


def effective_price_for_contract_qty(entry_cap_usd: float, ask: Optional[float]) -> float:
    """מחיר ליחידה לחישוב כמות: min(cap, Ask). ה-Ask צריך להיות עדכני (הקריאה לפני כמות ברמת _tick משתמשת ב-best ask מרוענן)."""
    if ask is None:
        return entry_cap_usd
    try:
        a = float(ask)
    except (TypeError, ValueError):
        return entry_cap_usd
    if not math.isfinite(a) or a < MIN_LEGIT_SHARE_PRICE_USD or a > MAX_LEGIT_SHARE_PRICE_USD:
        return entry_cap_usd
    return min(float(entry_cap_usd), a)


def dca_ref_price_from_ask(ask: float, entry_target_usd: float, cfg: StrategyConfig) -> float:
    """מחיר ייחוס לקביעת ה-quantity ול-limit_price בדא״ס.
    אם דחיסון אחוזים פעיל: Q נקבע לפי המחיר המונחה (X% מתחת ל-ask),
    אחרת לפי ask (כלומר התנהגות כמו היום — קניה קרובה ל-ask).
    """
    if not cfg.dca_discount_enabled or cfg.dca_discount_pct <= 0:
        return ask
    ref = ask * (1.0 - (cfg.dca_discount_pct / 100.0))
    # אותו כלל שהיה קודם: לא לתת ל-limit להיות מעל תקרת כניסה (מחושב על entry target)
    cap = entry_target_usd * 1.05
    if ref > cap:
        ref = cap
    # sanity: תמיד <= ask ל-buy limit
    if ref > ask:
        ref = ask
    return max(ref, 0.0)


async def fetch_best_bid_ask(token_id: str) -> tuple[Optional[float], Optional[float]]:
    async with httpx.AsyncClient() as client:
        try:
            book = await get_clob_book(client, token_id)
        except Exception:
            return None, None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid = float(bids[0]["price"]) if bids else None
    ask = float(asks[0]["price"]) if asks else None
    return bid, ask


class StrategyRunner:
    def __init__(self, demo: DemoEngine):
        self.demo = demo
        self.rt = StrategyRuntime()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start_loop(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    def stop_loop(self) -> None:
        self._stop.set()

    def sync_runtime_after_demo_positions_cleared(self) -> None:
        """קורא ל־sync_after_demo_positions_cleared על ה-runtime (אחרי איפוס דמו)."""
        self.rt.sync_after_demo_positions_cleared()
        self.demo.state.loss_recovery_streak = 0
        self.demo.state.loss_recovery_multiplier = 1.0
        self.demo.save()
        self.rt.log_event(
            "שחזור הפסד: איפוס מצב (איפוס חשבון / ניקוי סטטיסטיקה) — מכפיל 1.00×, רצף 0"
        )

    def _mark_first_strategy_buy_if_needed(self) -> None:
        """שעון שידור: רק בכניסת BUY ראשונה בלולאת האסטרטגיה (לא גידור רגל 2)."""
        if self.rt.strategy_first_buy_ts is None:
            self.rt.strategy_first_buy_ts = time.time()

    def _effective_investment_usd(self, cfg: StrategyConfig) -> float:
        if not cfg.loss_recovery_enabled:
            return float(cfg.investment_usd)
        m = float(self.demo.state.loss_recovery_multiplier)
        if not math.isfinite(m) or m < 1.0:
            m = 1.0
        return float(cfg.investment_usd) * m

    def _live_trading_ok(self) -> bool:
        if os.environ.get("POLYMARKET_LIVE", "").strip().lower() in ("0", "false", "no", "off"):
            return False
        return bool(os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip())

    async def approve_pending(self, live: bool = False) -> dict[str, Any]:
        p = self.rt.pending_approval
        if not p:
            return {"ok": False, "error": "אין המתנה לאישור"}
        if p["action"] == "buy":
            ctx = dict(p.get("context") or {})
            cfg0 = self.rt.config
            if cfg0.loss_recovery_enabled:
                ctx["effective_investment_usd"] = self._effective_investment_usd(cfg0)
                ctx["loss_recovery_multiplier"] = float(self.demo.state.loss_recovery_multiplier)
            oms = float(ctx.get("order_min_size") or 5)
            n_raw = float(p["contracts"])
            ok_sz, n_adj, verr = validate_contracts_for_market(n_raw, oms, bump_if_needed=True)
            if not ok_sz:
                self.rt.log(f"נכשל: {verr}")
                return {"ok": False, "error": verr or "גודל לא תקין"}
            use_live = bool(live) and self._live_trading_ok()
            if use_live:
                lim = float(p.get("limit") or 1.0)
                lo = await live_clob.place_limit_order(
                    str(p["token"]),
                    lim,
                    n_adj,
                    "BUY",
                )
                if not lo.get("ok"):
                    err = lo.get("error", "כשל לייב")
                    self.rt.log(f"נכשל (לייב): {err}")
                    try:
                        from run_logging import log_error

                        log_error(str(err), {"context": "approve_pending_live_buy"})
                    except Exception:
                        pass
                    return {"ok": False, "error": err}
                fill = float(lo.get("price") or lim)
                r = self.demo.record_live_buy(
                    p["side"],
                    str(p["token"]),
                    n_adj,
                    fill,
                    context=ctx,
                )
            else:
                r = await self.demo.simulate_market_buy(
                    p["side"],
                    str(p["token"]),
                    n_adj,
                    limit_price=float(p.get("limit") or 1.0),
                    context=ctx,
                )
            if r.get("ok") and self.rt.config.dca_enabled:
                self.rt.dca_done_slices += 1
                self.rt.last_dca_ts = time.time()
                # עדכון מחיר כניסה בפועל (נדרש ל־DCA drop קשיח בין כניסות)
                tr = (r.get("trade") or {}) if isinstance(r.get("trade"), dict) else {}
                fill = float(tr.get("price") or 0.0)
                self.rt.dca_last_fill_price = fill if fill > 0 else None
            if r.get("ok"):
                self._mark_first_strategy_buy_if_needed()
                # עדכון מונים/מגבלות (בכניסה בפועל)
                tr = (r.get("trade") or {}) if isinstance(r.get("trade"), dict) else {}
                fill = float(tr.get("price") or 0.0)
                c = float(tr.get("contracts") or p.get("contracts") or 0.0)
                cost = fill * c * (1 + FEE_RATE)
                now = time.time()
                self.rt.entries_this_window += 1
                self.rt.notional_this_window += cost
                self.rt.trade_timestamps.append(now)
                tr = r.get("trade") or {}
                price = tr.get("price")
                sid = tr.get("session_id")
                cfg_a = self.rt.config
                lr_a = ""
                if cfg_a.loss_recovery_enabled:
                    eff_a = self._effective_investment_usd(cfg_a)
                    m_a = float(self.demo.state.loss_recovery_multiplier)
                    lr_a = f" | שחזור הפסד: יעד אפקטיבי ${eff_a:.2f} (מכפיל {m_a:.2f}× על בסיס ${cfg_a.investment_usd:.2f})"
                self.rt.log_event(
                    (f"נכנס לעסקה (חצי־אוטו): {p['side']} ×{int(n_adj)} @ {float(price):.2f}{lr_a}"
                    if price is not None
                    else f"נכנס לעסקה (חצי־אוטו): {p['side']} ×{int(n_adj)}{lr_a}"),
                    session_id=sid,
                )
            else:
                err = r.get("error", "כשל")
                self.rt.log(f"נכשל: {err}")
                try:
                    from run_logging import log_error
                    log_error(str(err), {"context": "approve_pending", "action": p.get("action")})
                except Exception:
                    pass
            if r.get("ok"):
                self.rt.pending_approval = None
            return r
        if p["action"] == "hedge":
            ctx = p.get("context") or {}
            oms = float(ctx.get("order_min_size") or 5)
            n_raw = float(p["contracts"])
            ok_sz, n_adj, verr = validate_contracts_for_market(n_raw, oms, bump_if_needed=True)
            if not ok_sz:
                self.rt.log(f"גידור נכשל: {verr}")
                return {"ok": False, "error": verr or "גודל לא תקין"}
            use_live = bool(live) and self._live_trading_ok()
            if use_live:
                _, ask = await fetch_best_bid_ask(str(p["token"]))
                if ask is None:
                    self.rt.log("גידור לייב: אין Ask")
                    return {"ok": False, "error": "אין Ask לגידור"}
                ask_f = float(ask)
                lo = await live_clob.place_limit_order(str(p["token"]), ask_f, n_adj, "BUY")
                if not lo.get("ok"):
                    err = lo.get("error", "כשל לייב")
                    self.rt.log(f"גידור לייב נכשל: {err}")
                    return {"ok": False, "error": err}
                fill = float(lo.get("price") or ask_f)
                r = self.demo.record_live_buy(
                    p["side"], str(p["token"]), n_adj, fill, context=ctx
                )
            else:
                r = await self.demo.simulate_market_buy(
                    p["side"], str(p["token"]), n_adj, context=ctx
                )
            if r.get("ok"):
                self.rt.hedge_leg2_done = True
            tr = r.get("trade") or {}
            sid = tr.get("session_id")
            if not r.get("ok"):
                try:
                    from run_logging import log_error
                    log_error(r.get("error", "גידור נכשל"), {"session_id": sid})
                except Exception:
                    pass
            self.rt.log_event("אושר גידור" if r.get("ok") else f"גידור נכשל: {r.get('error')}", session_id=sid)
            if r.get("ok"):
                self.rt.pending_approval = None
            return r
        return {"ok": False}

    async def reject_pending(self) -> None:
        self.rt.pending_approval = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                # לפעמים str(e) ריק; נשמור repr כדי לקבל סוג/פרטים.
                self.rt.log(f"שגיאה: {e!r}")
                try:
                    from run_logging import log_error
                    log_error(repr(e), {"context": "_tick"})
                except Exception:
                    pass
            await asyncio.sleep(1.0)

    def _prune_trade_timestamps(self, now: float) -> None:
        """שומר רק עסקאות מהשעה האחרונה (למגבלת trades/hour)."""
        cutoff = now - 3600.0
        self.rt.trade_timestamps = [t for t in self.rt.trade_timestamps if t >= cutoff]

    def _entry_limits_ok(self, *, now: float, cfg: StrategyConfig, planned_cost_usd: float) -> bool:
        """בדיקות מגבלות בטיחות (ללא SL). מחזיר True אם מותר להיכנס."""
        self._prune_trade_timestamps(now)
        if cfg.max_trades_per_hour > 0 and len(self.rt.trade_timestamps) >= cfg.max_trades_per_hour:
            self.rt.status(
                f"סטטוס: הגיע למקס׳ עסקאות לשעה ({cfg.max_trades_per_hour}) — עצירה זמנית",
                key="limit_trades_per_hour",
            )
            return False
        if cfg.max_entries_per_window > 0 and self.rt.entries_this_window >= cfg.max_entries_per_window:
            self.rt.status(
                f"סטטוס: הגיע למקס׳ כניסות בחלון ({cfg.max_entries_per_window}) — לא נכנסים יותר בחלון הזה",
                key="limit_entries_per_window",
            )
            return False
        if cfg.max_notional_per_window_usd > 0 and (
            self.rt.notional_this_window + planned_cost_usd
        ) > cfg.max_notional_per_window_usd:
            self.rt.status(
                f"סטטוס: תקרת חשיפה בחלון (${cfg.max_notional_per_window_usd:.0f}) — נחסם (נדרש ~${planned_cost_usd:.2f})",
                key="limit_notional_per_window",
            )
            return False
        return True

    def _cooldown_allows_reentry(self, *, now: float, cfg: StrategyConfig, planned_side: str) -> bool:
        """Cooldown אחרי TP.
        ב-Hedge Mode: אם ה-TP היה בצד אחד (למשל Down), מותר להיכנס לצד השני מיד יחסית
        כדי לייצר 'החלפת רגל' במקום להמתין עוד cooldown על שני הצדדים.
        """
        if not self.rt.last_tp_ts:
            return True
        cooldown = max(0.0, cfg.reenter_cooldown_sec)
        if cooldown <= 0:
            return True
        if (now - self.rt.last_tp_ts) >= cooldown:
            return True

        if cfg.hedge_enabled and self.rt.last_tp_side and self.rt.last_tp_side != planned_side:
            return True
        return False

    def _time_gates(self, min_left: float, cfg: StrategyConfig) -> str:
        if min_left <= cfg.freeze_last_minutes:
            return "freeze"
        # אם המשתמש ביטל "אזור ביניים", לא נחסום כניסות בגלל min_minutes_for_entry,
        # ונשאיר רק את הקפיאה בדקה(ות) האחרונה(ות).
        if not cfg.intermediate_block_new_entries:
            return "ok"

        if min_left < cfg.min_minutes_for_entry and min_left > cfg.freeze_last_minutes:
            return "intermediate"
        return "ok"

    async def _tick(self) -> None:
        mode = self.rt.mode
        if mode == "off":
            return
        try:
            import main as _engine_main

            _engine_main._ensure_bot_run_session_if_active()
        except Exception:
            pass
        # Heartbeat שמראה שהמנוע ממשיך לרוץ גם אם בינתיים אין status מפורש.
        self.rt.last_tick_ts = time.time()
        m = await discover_active_btc_window(self.rt.config.btc_window)
        if not m:
            self.rt.log(f"לא נמצא שוק BTC Up/Down חלון {self.rt.config.btc_window} פעיל")
            return
        if m.epoch != self.rt.current_epoch:
            # לפני מעבר חלון: פירוק פוזיציות מחלון קודם (SETTLE_WIN / SETTLE_LOSS / …)
            settlement_trades: list[dict[str, Any]] = []
            if self.rt.current_epoch != 0:
                from market_discovery import window_step_sec

                settlement_trades = await self.demo.expire_all_outside_tokens(
                    (m.token_up, m.token_down),
                    context={
                        "settled_epoch": self.rt.current_epoch,
                        "settled_window_sec": window_step_sec(self.rt.config.btc_window),
                        "epoch": m.epoch,
                        "slug": m.slug,
                        "reason": "EXPIRE_0 rollover",
                    },
                )
                cfg_lr = self.rt.config
                if settlement_trades:
                    has_loss = any(float(t.get("realized_pnl") or 0) < 0 for t in settlement_trades)
                    if cfg_lr.loss_recovery_enabled:
                        from loss_recovery import apply_loss_recovery_from_settlements

                        lr_lines = apply_loss_recovery_from_settlements(
                            self.demo.state,
                            enabled=True,
                            step_pct=cfg_lr.loss_recovery_step_pct,
                            every_n_losses=cfg_lr.loss_recovery_every_n_losses,
                            max_multiplier=cfg_lr.loss_recovery_max_multiplier,
                            settlement_trades=settlement_trades,
                        )
                        self.demo.save()
                        for line in lr_lines:
                            self.rt.log_event(line)
                    elif has_loss:
                        self.rt.log_event(
                            "שחזור הפסד: כבוי בהגדרות המנוע — פירוק עם הפסד לא מגדיל מכפיל ולא משנה סכום לסלייס. "
                            "הפעל «שחזור אחרי הפסד», לחץ שמור הגדרות, והפעל מחדש את המנוע אם צריך לטעון config מהדיסק."
                        )
            self.rt.log(f"מעבר חלון → {m.slug}")
            self.rt.current_epoch = m.epoch
            self.rt.dca_done_slices = 0
            self.rt.last_dca_ts = 0.0
            self.rt.dca_last_fill_price = None
            self.rt.hedge_leg2_done = False
            self.rt.pending_approval = None
            self.rt.entries_this_window = 0
            self.rt.notional_this_window = 0.0
            self.rt.tp_happened_this_window = False
            self.rt.last_tp_ts = 0.0
            self.rt.last_tp_side = None
            self.rt._last_book_log_ts = 0.0

        sec_left = seconds_until_window_end(m.epoch, m.window_sec)
        min_left = sec_left / 60.0
        cfg = self.rt.config
        token_up, token_down = m.token_up, m.token_down
        bid_u, ask_u = await fetch_best_bid_ask(token_up)
        bid_d, ask_d = await fetch_best_bid_ask(token_down)
        missing: list[str] = []
        if ask_u is None:
            missing.append("Up")
        if ask_d is None:
            missing.append("Down")
        # נאסוף הערות המתנה — נשלב בהודעת limit fill כדי לא להציף 3 סטטוסים שונים בכל טיק
        wait_parts: list[str] = []
        if missing:
            wait_parts.append(f"ספר חסר ({', '.join(missing)})")
        # יומן מחירי שוק (אופציונלי) — כדי לראות שהבוט מסתכל על Polymarket בכל טיק
        if cfg.book_log_interval_sec and cfg.book_log_interval_sec > 0:
            log_now = time.time()
            if log_now - self.rt._last_book_log_ts >= float(cfg.book_log_interval_sec):
                self.rt._last_book_log_ts = log_now
                self.rt.log(
                    f"שוק CLOB: Up Ask {_fmt_px(ask_u)}/Bid {_fmt_px(bid_u)} · Down Ask {_fmt_px(ask_d)}/Bid {_fmt_px(bid_d)}"
                )
        gate_ctx = self._time_gates(min_left, cfg)
        min_c = max(cfg.min_contracts, int(math.ceil(float(m.order_min_size))))
        base_ctx: dict[str, Any] = {
            "epoch": m.epoch,
            "slug": m.slug,
            "gate": gate_ctx,
            "min_left_sec": sec_left,
            "ask_u": ask_u,
            "bid_u": bid_u,
            "ask_d": ask_d,
            "bid_d": bid_d,
            "order_min_size": float(m.order_min_size),
            "window_sec": int(m.window_sec),
            "btc_window": cfg.btc_window,
        }

        # TP
        best_near_tp: tuple[float, str] | None = None  # (missing_pct, label)
        # אם DCA פעיל: לא נותנים ל־TP לסגור עד שסיימנו את כל הסלייסים. חריגים:
        # 1) דקה אחרונה — אי־אפשר להוסיף DCA; 2) רווח ≥ dca_tp_override_pct — מונע לאבד רווח ענק.
        dca_locked = cfg.dca_enabled and self.rt.dca_done_slices < cfg.dca_slices
        tp_allowed_base = not dca_locked or (gate_ctx == "freeze")
        if not tp_allowed_base:
            wait_parts.append(f"TP נעול עד DCA ({self.rt.dca_done_slices}/{cfg.dca_slices})")
        for p in list(self.demo.state.positions):
            b, _ = await fetch_best_bid_ask(p.token_id)
            if b is None:
                continue
            upnl = self.demo.unrealized_pnl_pct(p.token_id, b)
            tp_allowed = tp_allowed_base or (
                dca_locked and upnl is not None and upnl >= cfg.dca_tp_override_pct
            )
            if upnl is not None and tp_allowed and cfg.near_tp_pct > 0:
                missing = cfg.take_profit_pct - upnl
                if 0 < missing <= cfg.near_tp_pct:
                    # מצטברים רק את הכי "קרוב" כדי לא להציף
                    label = p.side
                    if best_near_tp is None or missing < best_near_tp[0]:
                        best_near_tp = (missing, label)
            if tp_allowed and upnl is not None and upnl >= cfg.take_profit_pct * (1 + 2 * FEE_RATE):
                tp_ctx = dict(base_ctx)
                tp_ctx["reason"] = f"TP {p.side}: upnl={upnl:.2f}% >= {cfg.take_profit_pct:.2f}%"
                pos_contracts = float(p.contracts)
                if pos_contracts < float(m.order_min_size):
                    self.rt.log(
                        f"TP: פוזיציה {pos_contracts} מתחת למינימום השוק {m.order_min_size} — דילוג"
                    )
                    continue
                if self._live_trading_ok():
                    _, bid_tp = await fetch_best_bid_ask(p.token_id)
                    if bid_tp is None:
                        continue
                    lo = await live_clob.place_limit_order(
                        p.token_id, float(bid_tp), pos_contracts, "SELL",
                    )
                    if not lo.get("ok"):
                        self.rt.log(f"TP לייב: {lo.get('error', 'כשל')}")
                        continue
                    fill_sell = float(lo.get("price") or bid_tp)
                    r = await self.demo.record_live_sell(p.token_id, fill_sell, context=tp_ctx)
                else:
                    r = await self.demo.simulate_sell_all(p.token_id, context=tp_ctx)
                if r.get("ok"):
                    self.rt.record_tp(cfg=cfg, side=p.side)
                    if cfg.loss_recovery_enabled:
                        self.demo.state.loss_recovery_streak = 0
                        self.demo.state.loss_recovery_multiplier = 1.0
                        self.demo.save()
                    tr = r.get("trade") or {}
                    price = tr.get("price")
                    peak = tr.get("peak_unrealized_pct")
                    trough = tr.get("trough_unrealized_pct")
                    sid = tr.get("session_id")
                    self.rt.log_event(
                        f"TP {p.side}: יציאה @ {float(price) * 100:.1f}¢ (~{upnl:.1f}% מול עלות)"
                        if price is not None
                        else f"TP {p.side}: יציאה ~{upnl:.1f}% מול עלות",
                        session_id=sid,
                    )
                    if cfg.loss_recovery_enabled:
                        self.rt.log_event(
                            "שחזור הפסד: איפוס אחרי TP — מכפיל 1.00×, רצף הפסדים 0 (חזרה לסכום בסיס לכניסה הבאה)",
                            session_id=sid,
                        )
                    if peak is not None or trough is not None:
                        peak_s = f"{peak:.1f}%" if peak is not None else "—"
                        trough_s = f"{trough:.1f}%" if trough is not None else "—"
                        self.rt.log_event(
                            f"בזמן החזקה (מול עלות): שיא {peak_s} | שפל {trough_s}",
                            session_id=sid,
                        )

        # סימון תיק לפי bid כדי שסטטיסטיקה תראה הפסד/רווח גם לפני יציאה
        await self.demo.mark_to_market()

        gate = self._time_gates(min_left, cfg)
        sid = None
        for pos in self.demo.state.positions:
            sid = self.demo._session_by_token.get(pos.token_id)
            if sid:
                break
        if gate == "freeze":
            self.rt.status(
                f"סטטוס: דקה אחרונה ({min_left:.2f} דק׳) — לא נכנסים לעסקאות חדשות",
                key="freeze",
                session_id=sid,
            )
            return

        # אם אנחנו קרובים ל-TP (אבל עדיין לא הגיע) נציג סטטוס אינפורמטיבי
        if best_near_tp is not None:
            miss, label = best_near_tp
            self.rt.status(
                f"סטטוס: קרוב ל-TP ({label}) — חסר {miss:.2f}% ליעד",
                key="near_tp",
                session_id=sid,
            )

        pos_u = self.demo._position_idx(token_up) >= 0
        pos_d = self.demo._position_idx(token_down) >= 0
        if cfg.hedge_enabled and pos_u and pos_d:
            return

        # גידור — רגל שנייה
        if cfg.hedge_enabled and (pos_u ^ pos_d) and not self.rt.hedge_leg2_done:
            if ask_u is None or ask_d is None:
                self.rt.status(
                    "סטטוס: לא ניתן להעריך גידור (Ask חסר ב-Up/Down)",
                    key="hedge_book_missing",
                )
                return
            if ask_u + ask_d <= cfg.hedge_combined_ask_max:
                other_side = "Down" if pos_u else "Up"
                other_token = token_down if pos_u else token_up
                other_ask = ask_d if pos_u else ask_u
                eff_inv = self._effective_investment_usd(cfg)
                n = max(min_c, contracts_from_investment(
                    eff_inv / 2, other_ask, min_c
                ) or min_c)
                if mode == "semi" and not self.rt.pending_approval:
                    self.rt.pending_approval = {
                        "action": "hedge",
                        "contracts": float(n),
                        "token": other_token,
                        "side": other_side,
                        "context": {
                            **base_ctx,
                            "reason": f"hedge_leg2_pending:{other_side}",
                        },
                    }
                    self.rt.log(f"הצעת גידור: {other_side} ×{n} (סכום Ask משולב {ask_u+ask_d:.3f})")
                elif mode == "auto":
                    hedge_ctx = dict(base_ctx)
                    hedge_ctx["reason"] = f"hedge_leg2:{other_side}"
                    ok_h, n_h, verr_h = validate_contracts_for_market(
                        float(n), float(m.order_min_size), bump_if_needed=True
                    )
                    if not ok_h:
                        self.rt.status(
                            f"סטטוס: גידור — {verr_h}",
                            key="hedge_exchange_min",
                        )
                        return
                    n = n_h
                    if self._live_trading_ok():
                        lo = await live_clob.place_limit_order(
                            str(other_token), float(other_ask), float(n), "BUY"
                        )
                        if not lo.get("ok"):
                            self.rt.log(f"גידור אוטו (לייב): {lo.get('error', 'כשל')}")
                            return
                        fill_h = float(lo.get("price") or other_ask)
                        r = self.demo.record_live_buy(
                            other_side,
                            str(other_token),
                            float(n),
                            fill_h,
                            context=hedge_ctx,
                        )
                    else:
                        r = await self.demo.simulate_market_buy(
                            other_side, other_token, float(n), context=hedge_ctx
                        )
                    if r.get("ok"):
                        self.rt.hedge_leg2_done = True
                        tr = r.get("trade") or {}
                        self.rt.log_event("גידור (אוטו): רגל 2", session_id=tr.get("session_id"))
            return

        if gate == "intermediate":
            # גם אם יש כבר פוזיציה קיימת (לדוגמה DCA), עדיין נעדכן את last_status כדי שלא ייראה "תקוע".
            # היומן בפועל לא יספאם בגלל key קבוע.
            if not (pos_u or pos_d):
                msg = f"סטטוס: אזור ביניים ({min_left:.2f} דק׳) — בלי כניסות חדשות (מוגדר)"
            else:
                msg = f"סטטוס: אזור ביניים ({min_left:.2f} דק׳) — מנהל פוזיציה קיימת (אין כניסות חדשות)"
            self.rt.status(msg, key="intermediate", session_id=sid)
            return

        if cfg.side_preference == "Down":
            if ask_d is None:
                self.rt.status("סטטוס: Ask חסר ל-Down — לא ניתן להיכנס", key="book_missing_entry_down")
                return
            side, token, ask = "Down", token_down, ask_d
        elif cfg.side_preference == "Up":
            if ask_u is None:
                self.rt.status("סטטוס: Ask חסר ל-Up — לא ניתן להיכנס", key="book_missing_entry_up")
                return
            side, token, ask = "Up", token_up, ask_u
        else:
            # signal: צריך את שני ה-Ask כדי לבחור צד
            # אם יש כבר פוזיציה קיימת (ב-DCA למשל) — ממשיכים באותו צד כדי לא לקטוע רצף סלייסים.
            if pos_u and not pos_d:
                if ask_u is None:
                    self.rt.status("סטטוס: Ask חסר ל-Up — לא ניתן להמשיך DCA", key="book_missing_entry_up")
                    return
                side, token, ask = "Up", token_up, ask_u
            elif pos_d and not pos_u:
                if ask_d is None:
                    self.rt.status("סטטוס: Ask חסר ל-Down — לא ניתן להמשיך DCA", key="book_missing_entry_down")
                    return
                side, token, ask = "Down", token_down, ask_d
            else:
                if ask_u is None or ask_d is None:
                    self.rt.status(
                        "סטטוס: Ask חסר ל-2 הצדדים — לא ניתן לבחור צד (signal)",
                        key="book_missing_entry_signal",
                    )
                    return
                side, token, ask = (
                    ("Up", token_up, ask_u)
                    if ask_u <= ask_d
                    else ("Down", token_down, ask_d)
                )

        # Ask מחוץ לטווח 0.01–0.99 = ספר דליק / תקלה — לא מחשבים כניסה
        if ask is not None:
            af = float(ask)
            if af < MIN_LEGIT_SHARE_PRICE_USD or af > MAX_LEGIT_SHARE_PRICE_USD:
                self.rt.status(
                    f"סטטוס: Ask {side} לא תקין ({_fmt_px(ask)}) — מחוץ לטווח "
                    f"{MIN_LEGIT_SHARE_PRICE_USD}–{MAX_LEGIT_SHARE_PRICE_USD} (נזילות/ספר)",
                    key="ask_out_of_range",
                    session_id=self.demo._session_by_token.get(token),
                    repeat_interval_sec=8.0,
                )
                return

        # Ask מעודכן מיד לפני חישוב כמות — Ask בתחילת הטיק עלול להיות ישן; בדמו/לייב המילוי משתמש ב-best ask מחדש
        _, ask_entry = await fetch_best_bid_ask(str(token))
        if ask_entry is not None:
            try:
                ae = float(ask_entry)
            except (TypeError, ValueError):
                ae = float("nan")
            if math.isfinite(ae) and MIN_LEGIT_SHARE_PRICE_USD <= ae <= MAX_LEGIT_SHARE_PRICE_USD:
                ask = ask_entry

        price_usd = cfg.entry_price_cents / 100.0
        if cfg.dca_enabled:
            # cap לכניסה הבאה: לא עוברים את המחיר הרצוי,
            # ובסלייס הבא אנחנו גם מקיימים ירידה קשיחה מהכניסה הקודמת.
            dca_drop_factor = 1.0
            if cfg.dca_discount_enabled and cfg.dca_discount_pct > 0:
                dca_drop_factor = max(0.0, 1.0 - (cfg.dca_discount_pct / 100.0))

            entry_cap_price = price_usd
            if self.rt.dca_done_slices > 0 and self.rt.dca_last_fill_price:
                entry_cap_price = min(
                    price_usd,
                    float(self.rt.dca_last_fill_price) * float(dca_drop_factor),
                )
            if entry_cap_price < MIN_LEGIT_SHARE_PRICE_USD or entry_cap_price > MAX_LEGIT_SHARE_PRICE_USD:
                self.rt.status(
                    f"סטטוס: DCA — cap מחיר מחוץ לטווח ({entry_cap_price:.4f}$) — לא מוסיפים סלייס "
                    f"(טווח {MIN_LEGIT_SHARE_PRICE_USD}–{MAX_LEGIT_SHARE_PRICE_USD}$)",
                    key="dca_cap_out_of_range",
                    session_id=self.demo._session_by_token.get(token),
                    repeat_interval_sec=10.0,
                )
                return

            eff_inv = self._effective_investment_usd(cfg)
            per = eff_inv / max(1, cfg.dca_slices)
            # נחשב כמות שמרנית כך שהעמלות לא "יאכלו" את היתרה בסלייס הבא.
            safe_per = per / (1.0 + FEE_RATE)
            qty_px = effective_price_for_contract_qty(entry_cap_price, ask)
            n = contracts_from_investment(safe_per, qty_px, min_c)
            now = time.time()
            if self.rt.dca_done_slices >= cfg.dca_slices:
                return
            if self.rt.dca_done_slices > 0 and now - self.rt.last_dca_ts < cfg.dca_interval_sec:
                rem = max(0.0, cfg.dca_interval_sec - (now - self.rt.last_dca_ts))
                wp = (" · ".join(wait_parts) + " · ") if wait_parts else ""
                self.rt.status(
                    f"סטטוס: {wp}המתנה {rem:.0f}s לסלייס הבא (DCA interval)",
                    key="dca_interval_wait",
                    session_id=self.demo._session_by_token.get(token),
                    repeat_interval_sec=4.0,
                )
                return
        else:
            eff_inv = self._effective_investment_usd(cfg)
            qty_px = effective_price_for_contract_qty(price_usd, ask)
            n = contracts_from_investment(eff_inv, qty_px, min_c)
            entry_cap_price = price_usd
            if price_usd < MIN_LEGIT_SHARE_PRICE_USD or price_usd > MAX_LEGIT_SHARE_PRICE_USD:
                self.rt.status(
                    f"סטטוס: מחיר כניסה מוגדר מחוץ לטווח ({price_usd:.4f}$) — "
                    f"הגדר entry_price_cents בין {MIN_LEGIT_SHARE_PRICE_USD * 100:.0f} ל־{MAX_LEGIT_SHARE_PRICE_USD * 100:.0f} (סנטים)",
                    key="entry_target_out_of_range",
                    session_id=self.demo._session_by_token.get(token),
                    repeat_interval_sec=15.0,
                )
                return
        if n < min_c:
            self.rt.status(
                f"סטטוס: סכום/מחיר לא מספיקים למינ׳ {min_c} חוזים (מחושב {n})",
                key="size_too_small",
            )
            return
        ok_ex, n_use, ex_err = validate_contracts_for_market(
            float(n), float(m.order_min_size), bump_if_needed=True
        )
        if not ok_ex:
            self.rt.status(
                f"סטטוס: {ex_err}",
                key="exchange_min",
            )
            return
        n = n_use
        eff_inv_snapshot = self._effective_investment_usd(cfg)
        lr_mult_snapshot = float(self.demo.state.loss_recovery_multiplier)
        entry_sid = self.demo._session_by_token.get(token)
        # מפתח דה-דופ אחד לכל "לפני כניסה" באותו סלייס DCA — מונע:
        # (א) שורה חוזרת כל ~6s עם אותו טקסט; (ב) קפיצה ביומן כשעוברים מ"קרוב לכניסה" ל"רק TP נעול"
        # באותו טיק (שינוי Ask מעל/מתחת ל-cap) — אותו מפתח = לא שורת יומן נוספת, אבל last_status מתעדכן.
        prebuy_status_key = f"prebuy:{token}:{self.rt.dca_done_slices}"
        # cap בדולרים הוא entry_cap_price (ב-DCA יכול להיות נמוך מ-entry_price_cents); הסנטים בטקסט חייבים להתאים.
        cap_display_cents = entry_cap_price * 100.0
        prefix = (" · ".join(wait_parts) + " · ") if wait_parts else ""
        if ask > entry_cap_price:
            # לא נחסום כניסות: אנחנו נצמיד limit ל־entry_cap_price כך שה-fill לא יהיה מעל המחיר שביקשת.
            if cfg.near_entry_pct > 0:
                over_pct = ((ask / entry_cap_price) - 1.0) * 100.0 if entry_cap_price > 0 else 999.0
                if 0 < over_pct <= cfg.near_entry_pct:
                    self.rt.status(
                        f"סטטוס: {prefix}קרוב לכניסה ({side}) — Ask מעל ה-cap ב-{over_pct:.2f}% (~{cap_display_cents:.0f}¢)",
                        key=prebuy_status_key,
                        session_id=entry_sid,
                        repeat_interval_sec=14.0,
                    )
                else:
                    self.rt.status(
                        f"סטטוס: {prefix}ממתין ל-limit fill — Ask Up {_fmt_px(ask_u)}/Down {_fmt_px(ask_d)} · "
                        f"צד {side} Ask {_fmt_px(ask)} — cap {entry_cap_price:.2f}$ (~{cap_display_cents:.0f}¢)",
                        key=prebuy_status_key,
                        session_id=entry_sid,
                        repeat_interval_sec=14.0,
                    )
            else:
                self.rt.status(
                    f"סטטוס: {prefix}ממתין ל-limit fill — Ask Up {_fmt_px(ask_u)}/Down {_fmt_px(ask_d)} · "
                    f"צד {side} Ask {_fmt_px(ask)} — cap {entry_cap_price:.2f}$ (~{cap_display_cents:.0f}¢)",
                    key=prebuy_status_key,
                    session_id=entry_sid,
                    repeat_interval_sec=14.0,
                )
        elif wait_parts:
            self.rt.status(
                f"סטטוס: {' · '.join(wait_parts)}",
                key=prebuy_status_key,
                session_id=entry_sid,
                repeat_interval_sec=14.0,
            )

        if self.demo._position_idx(token) >= 0 and not cfg.dca_enabled:
            return
        if cfg.dca_enabled and self.demo._position_idx(token) < 0 and self.rt.dca_done_slices > 0:
            return

        now = time.time()
        # אם המשתמש לא רוצה כניסה מחדש אחרי TP — נחסום רה־כניסה בתוך אותו חלון (אבל לא את הכניסה הראשונה).
        if self.rt.tp_happened_this_window and not cfg.auto_reenter_after_tp and not (pos_u or pos_d):
            self.rt.status(
                "סטטוס: רה־כניסה אחרי TP כבויה — ממתין לחלון הבא",
                key="reenter_disabled",
            )
            return

        # cooldown אחרי TP כדי לא להיכנס מיד שוב ולהיגרר לספאם
        if not self._cooldown_allows_reentry(now=now, cfg=cfg, planned_side=side):
            self.rt.status(
                f"סטטוס: cooldown אחרי TP ({cfg.reenter_cooldown_sec:.0f}s) — ממתין ({cfg.reenter_cooldown_sec - (now - self.rt.last_tp_ts):.1f}s)",
                key="reenter_cooldown",
            )
            return

        # ה-limit תמיד לא עובר את הסנטים שביקשת (או פחות),
        # וב-DCA הוא גם מקיים drop קשיח מהכניסה הקודמת בפועל.
        lim = min(ask * 1.01, entry_cap_price)
        planned_cost = lim * float(n) * (1 + FEE_RATE)
        if not self._entry_limits_ok(now=now, cfg=cfg, planned_cost_usd=planned_cost):
            return
        entry_over_pct = (ask / price_usd - 1.0) * 100.0 if price_usd > 0 else 0.0
        entry_ctx = dict(base_ctx)
        entry_ctx.update(
            {
                "reason": f"entry_ok:{side} gate={gate} ask_vs_target={entry_over_pct:.2f}%",
                "entry_target_usd": price_usd,
                "limit_price": lim,
            }
        )
        if cfg.loss_recovery_enabled:
            entry_ctx["effective_investment_usd"] = eff_inv_snapshot
            entry_ctx["loss_recovery_multiplier"] = lr_mult_snapshot
        if mode == "semi":
            if not self.rt.pending_approval:
                self.rt.pending_approval = {
                    "action": "buy",
                    "side": side,
                    "contracts": float(n),
                    "token": token,
                    "limit": lim,
                    "ask": ask,
                    "context": entry_ctx,
                }
                self.rt.log(f"ממתין לאישור: {side} ×{n} @~{ask:.2f}")
            return

        if self._live_trading_ok():
            lo = await live_clob.place_limit_order(str(token), float(lim), float(n), "BUY")
            if not lo.get("ok"):
                self.rt.log(f"כניסה אוטומטית (לייב) נכשלה: {lo.get('error', 'כשל')}")
                return
            fill_a = float(lo.get("price") or lim)
            r = self.demo.record_live_buy(side, str(token), float(n), fill_a, context=entry_ctx)
        else:
            r = await self.demo.simulate_market_buy(
                side, token, float(n), limit_price=lim, context=entry_ctx
            )
        if r.get("ok"):
            self._mark_first_strategy_buy_if_needed()
            # עדכון מונים/מגבלות
            tr = (r.get("trade") or {}) if isinstance(r.get("trade"), dict) else {}
            fill = float(tr.get("price") or 0.0)
            c = float(tr.get("contracts") or n or 0.0)
            cost = fill * c * (1 + FEE_RATE)
            self.rt.entries_this_window += 1
            self.rt.notional_this_window += cost
            self.rt.trade_timestamps.append(time.time())
            tr = r.get("trade") or {}
            price = tr.get("price")
            sid = tr.get("session_id")
            dca_part = f" — DCA {self.rt.dca_done_slices}/{cfg.dca_slices}" if cfg.dca_enabled else ""
            lr_part = ""
            if cfg.loss_recovery_enabled:
                lr_part = f" | שחזור הפסד: יעד אפקטיבי ${eff_inv_snapshot:.2f} (מכפיל {lr_mult_snapshot:.2f}× על בסיס ${cfg.investment_usd:.2f})"
            self.rt.log_event(
                (f"נכנס לעסקה (אוטומטי){dca_part}: {side} ×{n} @ {float(price):.2f}{lr_part}"
                if price is not None
                else f"נכנס לעסקה (אוטומטי){dca_part}: {side} ×{n}{lr_part}"),
                session_id=sid,
            )
            if cfg.dca_enabled:
                self.rt.dca_done_slices += 1
                self.rt.last_dca_ts = time.time()
                # עדכון מחיר כניסה בפועל (נדרש ל־DCA drop קשיח בין כניסות)
                self.rt.dca_last_fill_price = fill if fill > 0 else None
