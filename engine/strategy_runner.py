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

# המתנה בין טיקים — ברירת מחדל מהירה יותר מ־0.25; ניתן לעדכן: STRATEGY_TICK_SLEEP_SEC=0.08
_STRATEGY_TICK_SLEEP = max(0.05, min(float(os.environ.get("STRATEGY_TICK_SLEEP_SEC", "0.12")), 2.0))


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
    # ביצוע: "limit" (GTC קלאסי) או "market" (FOK לכניסה, FAK ליציאה + retry ladder).
    # מטרה: להבטיח ביצוע מידי ולמנוע פוזיציה תקועה כשהשוק מדלג על יעד ה-TP.
    order_mode: Literal["limit", "market"] = "limit"
    entry_slippage_pct: float = 2.0  # תקרת slippage לכניסת MARKET BUY
    exit_slippage_pct: float = 5.0   # תקרת slippage ליציאת MARKET SELL (רחבה — עדיף לצאת)
    # Peak Watchdog: אם bid נגע פעם ב-TP ואז נפל ביותר מ-X% מהשיא, מוכר מידי
    # (גם אם עדיין מעל entry) — מונע "עסקה שהיתה ברווח והפכה להפסד".
    peak_watchdog_enabled: bool = True
    peak_retreat_exit_pct: float = 2.0
    # כמה retry לאחר FAK חלקי ביציאה (הרחבת slippage בכל retry)
    retry_max_attempts: int = 3
    # Hold-to-Resolution: אחרי ריבוי DCA וכאשר הכיוון ברור (bid גבוה),
    # לא לוקחים TP חלקי — מחזיקים עד סוף החלון כדי לקבל $1.00 ולכסות הפסדי DCA.
    # stop-loss דינמי מגן אם bid נופל מתחת לממוצע הכניסה המשוקלל.
    hold_to_resolution_enabled: bool = False
    hold_to_resolution_min_dca_slices: int = 2
    hold_to_resolution_min_price: float = 0.85
    hold_to_resolution_stop_loss_enabled: bool = True
    # גודל השקעה: "fixed" = סכום קבוע ב-$ (investment_usd);
    # "percent" = אחוז מגודל החשבון (equity_snapshot) כפול investment_pct_of_portfolio.
    investment_mode: Literal["fixed", "percent"] = "fixed"
    investment_pct_of_portfolio: float = 5.0
    # Follow Last Winner (FLW): כיוון הכניסה נגזר מתוצאת חלון/ות הקודמים.
    # forward = ממשיכים עם הצד שניצח; reverse = הופכים (mean reversion).
    # min_btc_drift_pct: אם הזזת BTC בחלון הקודם הייתה < X%, מתעלמים מהחלון (רעש).
    # אם אין history (חלון ראשון) או תיקו ב-lookback>1 → fallback ל-side_preference.
    follow_last_winner_enabled: bool = False
    follow_last_winner_lookback: int = 1
    follow_last_winner_mode: Literal["forward", "reverse"] = "forward"
    follow_last_winner_min_btc_drift_pct: float = 0.0


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
    # מצב "כסף אמיתי" נשלט מהממשק (לחיצה על "מעבר למסחר חי") ונשמר לדיסק.
    # POLYMARKET_LIVE env הוא kill-switch בלבד להגדרת שרת/פריסה — לא משמש כדי להפעיל לייב.
    live_trading: bool = False
    # Reconcile של הספר הפנימי מול היתרה/פוזיציות האמיתיות של Polymarket.
    _last_live_reconcile_ts: float = 0.0
    # כניסה אוטומטית לייב שנכשלה — מצמצמים חזרות על אותה שגיאה (לא משנה לוגיקת מסחר).
    _last_live_auto_entry_fail_ts: float = 0.0
    _last_live_auto_entry_fail_msg: str = ""
    _settled_token_ids: set = field(default_factory=set)
    _settled_token_ids_ts: float = 0.0
    # Peak Watchdog: {token_id: {"peak_bid": float, "tp_touched": bool, "tp_target": float}}
    # פוזיציה שעברה פעם את יעד ה-TP — אם ה-bid נופל אחורה, מוכרים מיד.
    _peak_state: dict = field(default_factory=dict)
    # Cooldown per-token על TP-SELL אחרי שגיאת "insufficient_onchain_balance":
    # מונע spam של 400-errors עד ש-reconcile יתקן את פער ה-ledger/chain.
    _tp_sell_cooldown_until: dict = field(default_factory=dict)

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
    from ws_price_stream import price_stream
    bid, ask = price_stream.get_best_bid_ask(token_id)
    if bid is not None or ask is not None:
        tp = price_stream.get_price(token_id)
        if tp and (time.time() - tp.ts) < 30.0:
            return bid, ask
    async with httpx.AsyncClient(timeout=6.0) as client:
        try:
            book = await get_clob_book(client, token_id)
        except Exception:
            return bid, ask
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    rest_bid = float(bids[0]["price"]) if bids else None
    rest_ask = float(asks[0]["price"]) if asks else None
    return rest_bid, rest_ask


class StrategyRunner:
    def __init__(self, demo: DemoEngine):
        self.demo = demo
        self.rt = StrategyRuntime()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # FIX #24: guard נגד זיהוי rollover כפול אם _tick רץ פעמיים בתוך אותה שנייה.
        self._rollover_lock: asyncio.Lock = asyncio.Lock()
        # FIX #22: שחזור DCA counters מ-state אם השרת קם אחרי קריסה באמצע DCA
        self._restore_dca_counters_from_state()

    def _restore_dca_counters_from_state(self) -> None:
        """משחזר את ה-DCA counters מ-DemoState (persisted) ל-StrategyRuntime (memory).

        אם השרת קם אחרי קריסה באמצע DCA, ה-counters בזיכרון אופסו ל-0 ב-__init__,
        אבל ה-state נשמר. שחזור מהדיסק מבטיח שלא נקנה את אותו slice פעמיים.
        """
        try:
            self.rt.dca_done_slices = int(getattr(self.demo.state, "dca_done_slices_persisted", 0) or 0)
            self.rt.last_dca_ts = float(getattr(self.demo.state, "dca_last_dca_ts_persisted", 0.0) or 0.0)
            self.rt.dca_last_fill_price = getattr(self.demo.state, "dca_last_fill_price_persisted", None)
            self.rt.current_epoch = int(getattr(self.demo.state, "dca_active_epoch_persisted", 0) or 0)
        except AttributeError:
            # state ישן ללא השדות החדשים — לא נורא, מתחילים מ-0
            pass

    def _persist_dca_counters(self) -> None:
        """שומר את ה-DCA counters ל-DemoState ולדיסק. נקרא אחרי כל מוטציה."""
        try:
            self.demo.state.dca_done_slices_persisted = int(self.rt.dca_done_slices or 0)
            self.demo.state.dca_last_dca_ts_persisted = float(self.rt.last_dca_ts or 0.0)
            self.demo.state.dca_last_fill_price_persisted = self.rt.dca_last_fill_price
            self.demo.state.dca_active_epoch_persisted = int(self.rt.current_epoch or 0)
            self.demo.save()
        except Exception:
            # שמירה לא אטומית — לא לעצור את המנוע בגלל זה
            pass

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

    def _investment_base_usd(self, cfg: StrategyConfig) -> float:
        mode = str(getattr(cfg, "investment_mode", "fixed") or "fixed").lower()
        if mode == "percent":
            pct = float(getattr(cfg, "investment_pct_of_portfolio", 0.0) or 0.0)
            if not math.isfinite(pct) or pct <= 0:
                pct = 0.0
            try:
                equity = float(self.demo.equity_snapshot_usd())
            except Exception:
                equity = float(self.demo.state.balance_usd)
            return max(0.0, equity) * (pct / 100.0)
        return float(cfg.investment_usd)

    def _investment_base_label(self, cfg: StrategyConfig) -> str:
        mode = str(getattr(cfg, "investment_mode", "fixed") or "fixed").lower()
        base = self._investment_base_usd(cfg)
        if mode == "percent":
            pct = float(getattr(cfg, "investment_pct_of_portfolio", 0.0) or 0.0)
            return f"${base:.2f} ({pct:.1f}% מהתיק)"
        return f"${base:.2f}"

    def _effective_investment_usd(self, cfg: StrategyConfig) -> float:
        base = self._investment_base_usd(cfg)
        if not cfg.loss_recovery_enabled:
            return base
        m = float(self.demo.state.loss_recovery_multiplier)
        if not math.isfinite(m) or m < 1.0:
            m = 1.0
        return base * m

    def _live_trading_ok(self) -> bool:
        # kill-switch לשרת/פריסה בלבד — POLYMARKET_LIVE=0 חוסם לחלוטין שליחת לייב
        if os.environ.get("POLYMARKET_LIVE", "").strip().lower() in ("0", "false", "no", "off"):
            return False
        # מצב "כסף אמיתי" נשלט מהממשק (לחיצה על "מעבר למסחר חי")
        if not bool(self.rt.live_trading):
            return False
        return bool(os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip())

    async def _live_close_outside_tokens(
        self,
        valid_tokens: tuple[str, str],
        *,
        context: dict[str, Any],
    ) -> list[str]:
        """במצב לייב, לפני פירוק החלון: שלח SELL אמיתי ב-best bid לכל פוזיציה שאינה
        בטוקנים הפעילים הנוכחיים. מצליח חלקית זה בסדר — את מה שלא נמכר אקטיבית יתפוס
        expire_all_outside_tokens כגיבוי חישובי, ולאחר מכן reconcile_live_state יסנכרן
        מול המציאות האמיתית.
        מחזיר רשימת token_id שנסגרו בהצלחה.
        """
        closed: list[str] = []
        positions_snapshot = [p for p in self.demo.state.positions if p.token_id not in valid_tokens]
        no_bid_count = 0
        sell_fail_msgs: list[str] = []
        for p in positions_snapshot:
            bid, _ = await fetch_best_bid_ask(p.token_id)
            if bid is None or bid <= 0:
                no_bid_count += 1
                continue
            cfg = self.rt.config
            lo = await live_clob.place_exit_order(
                p.token_id,
                float(p.contracts),
                float(bid),
                order_mode=getattr(cfg, "order_mode", "limit"),
                exit_slippage_pct=float(getattr(cfg, "exit_slippage_pct", 5.0)),
                retry_max_attempts=int(getattr(cfg, "retry_max_attempts", 3)),
            )
            if not lo.get("ok"):
                err = str(lo.get("error", "כשל"))
                if err not in sell_fail_msgs:
                    sell_fail_msgs.append(err)
                continue
            fill_sell = float(lo.get("fill_price") or lo.get("price") or bid)
            close_ctx = dict(context)
            close_ctx["reason"] = "EXPIRE_ACTIVE_CLOSE"
            close_ctx["execution"] = "live"
            sold_sz = float(lo.get("size") or p.contracts)
            try:
                rs = await self.demo.record_live_sell(
                    p.token_id,
                    fill_sell,
                    context=close_ctx,
                    contracts_sold=sold_sz,
                )
            except Exception as e:
                self.rt.log(f"record_live_sell כשל אחרי מילוי אמיתי: {e}")
                continue
            if not rs.get("ok"):
                continue
            if rs.get("full_exit", True):
                closed.append(p.token_id)
            self.rt.log(
                f"סגירה אקטיבית ({p.side}): {sold_sz:.2f} @ {fill_sell:.3f}"
                + ("" if rs.get("full_exit", True) else " (חלקי — נותרה פוזיציה)")
            )
        if no_bid_count:
            self.rt.log(
                f"סגירה אקטיבית: אין bid ל־{no_bid_count} פוזיציות מחוץ לחלון "
                "(לרוב שוק/ספר כבר לא פעילים אחרי סיום החלון) — גיבוי יפעל"
            )
        if sell_fail_msgs:
            for msg in sell_fail_msgs[:5]:
                self.rt.log(f"סגירה אקטיבית נכשלה: {msg} — גיבוי יפעל")
            if len(sell_fail_msgs) > 5:
                self.rt.log(
                    f"סגירה אקטיבית: עוד {len(sell_fail_msgs) - 5} סוגי שגיאות — גיבוי יפעל"
                )
        return closed

    async def _live_reconcile_if_enabled(
        self,
        *,
        context: Optional[dict[str, Any]] = None,
        force: bool = False,
        exclude_token_ids: Optional[set[str]] = None,
    ) -> None:
        """מושך יתרה + פוזיציות אמיתיות של Polymarket ומסנכרן את demo.state.
        קורה אחרי rollover ובקצב קבוע (כל ~2 דק׳) כסדר־גב נגד drift.
        exclude_token_ids — טוקנים שכבר פורקו ב-expire; לא להחזיר ב-reconcile.
        """
        if not self._live_trading_ok():
            return
        now = time.time()
        if not force and (now - float(self.rt._last_live_reconcile_ts or 0) < 120.0):
            return
        try:
            portfolio = await live_clob.fetch_live_portfolio(force=True)
        except Exception as e:
            self.rt.log(f"reconcile לייב: שגיאת fetch — {e}")
            return
        if not portfolio.get("ok"):
            return
        ctx = dict(context or {})
        ctx.setdefault("reason", "LIVE_RECONCILE")
        tr = self.demo.reconcile_live_state(
            portfolio.get("balance_usd"),
            list(portfolio.get("positions") or []),
            context=ctx,
            exclude_token_ids=exclude_token_ids,
        )
        self.rt._last_live_reconcile_ts = now
        if tr is not None:
            delta = float(tr.get("realized_pnl") or 0.0)
            extra = ""
            if abs(delta) >= 1000.0:
                extra = (
                    " — סנכרון יומן פנימי מול יתרת CLOB; "
                    "לא בהכרח תנועת כסף בודדת."
                )
            self.rt.log_event(f"reconcile לייב: דלתא יתרה {delta:+.2f}${extra}")

    async def approve_pending(self, live: Optional[bool] = None) -> dict[str, Any]:
        """מאשר פקודה ממתינה.

        מצב "כסף אמיתי" נקבע לפי `self.rt.live_trading` (מופעל מהממשק).
        הפרמטר `live` נשמר לתאימות אחורה: אם הועבר במפורש כ-False מהצד שני,
        הוא עדיין יוביל לסימולציה (ניתן לכפות סימולציה לבקשה בודדת).
        """
        p = self.rt.pending_approval
        if not p:
            return {"ok": False, "error": "אין המתנה לאישור"}
        # אם הצד השני לא העביר במפורש, קוראים את מצב הזמן־אמת מהמנוע.
        live_request = bool(self.rt.live_trading) if live is None else bool(live)
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
            use_live = live_request and self._live_trading_ok()
            if use_live:
                lim = float(p.get("limit") or 1.0)
                lo = await live_clob.place_entry_order(
                    str(p["token"]),
                    n_adj,
                    lim,
                    "BUY",
                    order_mode=getattr(cfg0, "order_mode", "limit"),
                    entry_slippage_pct=float(getattr(cfg0, "entry_slippage_pct", 2.0)),
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
                fill = float(lo.get("fill_price") or lo.get("price") or lim)
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
                # FIX #22: שמור ל-state אחרי כל DCA slice — שורד restart
                self._persist_dca_counters()
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
                    lr_a = f" | שחזור הפסד: יעד אפקטיבי ${eff_a:.2f} (מכפיל {m_a:.2f}× על בסיס {self._investment_base_label(cfg_a)})"
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
            use_live = live_request and self._live_trading_ok()
            if use_live:
                _, ask = await fetch_best_bid_ask(str(p["token"]))
                if ask is None:
                    self.rt.log("גידור לייב: אין Ask")
                    return {"ok": False, "error": "אין Ask לגידור"}
                ask_f = float(ask)
                cfg_h = self.rt.config
                lo = await live_clob.place_entry_order(
                    str(p["token"]),
                    n_adj,
                    ask_f,
                    "BUY",
                    order_mode=getattr(cfg_h, "order_mode", "limit"),
                    entry_slippage_pct=float(getattr(cfg_h, "entry_slippage_pct", 2.0)),
                )
                if not lo.get("ok"):
                    err = lo.get("error", "כשל לייב")
                    self.rt.log(f"גידור לייב נכשל: {err}")
                    return {"ok": False, "error": err}
                fill = float(lo.get("fill_price") or lo.get("price") or ask_f)
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
                self.rt.log(f"שגיאה: {e!r}")
                try:
                    from run_logging import log_error
                    log_error(repr(e), {"context": "_tick"})
                except Exception:
                    pass
            await asyncio.sleep(_STRATEGY_TICK_SLEEP)

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
        # המשך סלייסי DCA לא נספר כ«כניסה חדשה» למגבלה — אחרת max_entries=1 חוסם סלייס 2..N
        # ו-TP נשאר נעול לנצח (dca_done_slices < dca_slices).
        dca_mid_round = (
            cfg.dca_enabled
            and self.rt.dca_done_slices >= 1
            and self.rt.dca_done_slices < cfg.dca_slices
        )
        if (
            cfg.max_entries_per_window > 0
            and self.rt.entries_this_window >= cfg.max_entries_per_window
            and not dca_mid_round
        ):
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

    def _record_settlement_to_history(
        self,
        settlement_trades: list[dict[str, Any]],
        rollover_ctx: dict[str, Any],
    ) -> None:
        """כותב את תוצאת החלון ל-history.db מיד עם זיהוי rollover.

        משתמש בנתוני ה-settlement שכבר חושבו (resolved_outcome + settlement_btc_start/end),
        כך שאין צורך בקריאת רשת נוספת ואין סיכון לתוצאה שונה מ-auto_history_recorder.
        אם UPSERT (fix #18) פוגש record קיים — מעדכן רק שדות NULL (idempotent).
        """
        if not settlement_trades:
            return
        from history_tracker import record_window_result

        ep = rollover_ctx.get("settled_epoch")
        ws = rollover_ctx.get("settled_window_sec") or 300
        slug = rollover_ctx.get("slug") or ""
        if ep is None or not slug:
            # context לא שלם — לא נכשל, פשוט לא נרשום (recorder ירשום מאוחר).
            return
        # מצא את הראשון עם resolved_outcome (כל ה-settlement trades לאותו חלון יסכימו)
        resolved: Optional[str] = None
        btc_start: Optional[float] = None
        btc_end: Optional[float] = None
        for t in settlement_trades:
            ro = t.get("resolved_outcome")
            if ro in ("Up", "Down"):
                resolved = ro
                btc_start = t.get("settlement_btc_start")
                btc_end = t.get("settlement_btc_end")
                break
        if resolved is None:
            # אין settlement עם תוצאה ידועה — דחיה ל-auto_history_recorder
            return
        # epoch של החלון שהסתיים = settled_epoch מה-rollover_ctx. נשתמש ב-prev_slug
        # אבל ה-slug ב-rollover_ctx הוא של החלון החדש; נשנה אותו לפי epoch הישן.
        # פורמט slug: "btc-updown-5m-<epoch>" — אפשר לבנות אותו מחדש.
        # פשוט יותר: לוקח את ה-slug מהעסקה (היא נסגרה ביחס לחלון הישן).
        for t in settlement_trades:
            t_slug = t.get("slug")
            if t_slug:
                slug = t_slug
                break
        # אם slug עדיין לא נכון (slug חדש), נבנה אותו מתבנית
        if str(ep) not in slug:
            window_label = "5m" if int(ws) == 300 else "15m"
            slug = f"btc-updown-{window_label}-{ep}"
        try:
            saved = record_window_result(
                epoch=int(ep),
                slug=str(slug),
                window_sec=int(ws),
                side_won=resolved,
                btc_open=btc_start,
                btc_close=btc_end,
            )
            self.rt.log(
                f"היסטוריה: חלון {slug} נרשם={resolved} (settlement source) — UPSERT={saved}"
            )
        except Exception as e:
            self.rt.log(f"היסטוריה: כשל ברישום — {e!r}")

    def _resolve_follow_winner_side(self, cfg: StrategyConfig) -> Optional[str]:
        """מחזיר 'Up'/'Down' לפי תוצאת חלון/ות קודמים. None = fallback ל-side_preference.

        כללים:
        - lookback מנורמל ל-[1, 5].
        - אם lookback>1 לוקחים רוב; תיקו → None (fallback).
        - mode='reverse' הופך את התוצאה (mean reversion).
        - min_btc_drift_pct מסנן חלונות עם תזוזת BTC חלשה (רעש).
        - בלי history → None (fallback).
        """
        from history_tracker import get_last_window_winners
        from market_discovery import window_step_sec

        try:
            window_sec = window_step_sec(cfg.btc_window)
        except Exception:
            window_sec = 300
        lookback = int(getattr(cfg, "follow_last_winner_lookback", 1) or 1)
        lookback = max(1, min(5, lookback))
        min_drift = float(getattr(cfg, "follow_last_winner_min_btc_drift_pct", 0.0) or 0.0)
        winners = get_last_window_winners(
            window_sec=window_sec, limit=lookback, min_drift_pct=min_drift
        )
        if not winners:
            self.rt.status(
                "FLW: אין חלונות סגורים שעומדים בתנאים — fallback ל-side_preference",
                key="flw_no_history",
                repeat_interval_sec=60.0,
            )
            return None
        up_count = sum(1 for w in winners if w.get("side_won") == "Up")
        down_count = sum(1 for w in winners if w.get("side_won") == "Down")
        if up_count == down_count:
            self.rt.status(
                f"FLW: תיקו ב-{len(winners)} חלונות — fallback ל-side_preference",
                key="flw_tie",
                repeat_interval_sec=60.0,
            )
            return None
        direction = "Up" if up_count > down_count else "Down"
        mode = getattr(cfg, "follow_last_winner_mode", "forward")
        if mode == "reverse":
            direction = "Down" if direction == "Up" else "Up"
        return direction

    async def _settle_stale_positions_when_off(self) -> None:
        """Housekeeping ל-mode=='off' בלבד: סגירת פוזיציות תקועות מחלונות שהסתיימו.

        רקע: ב-_tick הרגיל יש בלוק rollover (line ~787) שמטפל בפוזיציות מחלון
        שעבר. הוא רץ רק כשmode!='off'. אם המשתמש כיבה את המנוע (או הבוט הופעל
        מחדש) בזמן שיש פוזיציה פתוחה, הפוזיציה נשארת תקועה ב-state בלי שאיש
        יסגור אותה — bug שגרם ל-trade #90 להישאר פתוח 153 שעות.

        הפונקציה הזו רצה רק כשmode=='off' (קוראים לה לפני return early של _tick),
        ומסדרת את המקרה הזה. אם mode!='off', הקוד המקורי יטפל ברגיל ויפעיל גם
        loss_recovery, reset של dca counters וכו'. לכן לא לקרוא לפונקציה הזו
        כשmode!='off' (כפילות).
        """
        if not self.demo.state.positions:
            return
        try:
            m = await discover_active_btc_window(self.rt.config.btc_window)
        except Exception:
            return
        if not m:
            return
        # יש פוזיציות. נבדוק אם token_id שלהן עדיין מתאים לשוק הפעיל. אם לא — לסגור.
        valid_tokens = {m.token_up, m.token_down}
        stale = [p for p in self.demo.state.positions if p.token_id not in valid_tokens]
        if not stale:
            return
        from market_discovery import window_step_sec

        rollover_ctx = {
            "settled_epoch": self.rt.current_epoch or None,
            "settled_window_sec": window_step_sec(self.rt.config.btc_window),
            "epoch": m.epoch,
            "slug": m.slug,
            "reason": "EXPIRE_0 stale_cleanup_while_off",
        }
        try:
            settlement_trades = await self.demo.expire_all_outside_tokens(
                (m.token_up, m.token_down),
                context=rollover_ctx,
            )
        except Exception as e:
            self.rt.log(f"stale_cleanup: {e!r}")
            return
        if settlement_trades:
            settled_tids = {t.get("token_id") or "" for t in settlement_trades}
            settled_tids.discard("")
            self.rt._settled_token_ids.update(settled_tids)
            self.rt._settled_token_ids_ts = time.time()
            self.rt.log(
                f"ניקוי תקופתי (mode=off): סגרתי {len(settlement_trades)} פוזיציות מחלון ישן"
            )

    async def _tick(self) -> None:
        mode = self.rt.mode
        if mode == "off":
            # mode כבוי — אבל פוזיציות מחלון ישן חייבות עדיין להיסגר.
            try:
                await self._settle_stale_positions_when_off()
            except Exception as e:
                self.rt.log(f"housekeeping_off: {e!r}")
            return
        try:
            import main as _engine_main

            _engine_main._ensure_bot_run_session_if_active()
        except Exception:
            pass

        # Heartbeat שמראה שהמנוע ממשיך לרוץ גם אם בינתיים אין status מפורש.
        self.rt.last_tick_ts = time.time()
        # Reconcile קבוע (לא יותר מ-1 ל-120 שניות) כדי לתפוס drift בין הספר הפנימי
        # ליתרה/פוזיציות האמיתיות של Polymarket — רץ רק במצב לייב.
        # ניקוי settled_token_ids אחרי שעה (Data API כבר מפסיק להחזיר אותם).
        if (
            self.rt._settled_token_ids
            and time.time() - self.rt._settled_token_ids_ts > 3600
        ):
            self.rt._settled_token_ids.clear()
        try:
            await self._live_reconcile_if_enabled(
                exclude_token_ids=self.rt._settled_token_ids or None,
            )
        except Exception:
            pass
        m = await discover_active_btc_window(self.rt.config.btc_window)
        if not m:
            self.rt.log(f"לא נמצא שוק BTC Up/Down חלון {self.rt.config.btc_window} פעיל")
            return
        if m.epoch != self.rt.current_epoch:
            # FIX #24: lock + double-check pattern — אם _tick רץ פעמיים בתוך מילישנייה
            # (race ב-async scheduler), רק הראשון יבצע את ה-rollover.
            async with self._rollover_lock:
                if m.epoch == self.rt.current_epoch:
                    # rollover כבר בוצע בקריאה אחרת. דלג.
                    return
            # לפני מעבר חלון: פירוק פוזיציות מחלון קודם (SETTLE_WIN / SETTLE_LOSS / …)
            settlement_trades: list[dict[str, Any]] = []
            if self.rt.current_epoch != 0:
                from market_discovery import window_step_sec

                rollover_ctx = {
                    "settled_epoch": self.rt.current_epoch,
                    "settled_window_sec": window_step_sec(self.rt.config.btc_window),
                    "epoch": m.epoch,
                    "slug": m.slug,
                    "reason": "EXPIRE_0 rollover",
                }
                # כדי ש־SETTLE_* יופיעו בלשונית «סטטיסטיקה לייב» (מסננים execution=live)
                if self._live_trading_ok():
                    rollover_ctx["execution"] = "live"
                # לייב: לפני שאנחנו מסתמכים על פירוק BTC-פרוקסי, ננסה לסגור אקטיבית
                # את הפוזיציות האמיתיות ב-CLOB. מה שנכשל נופל לגיבוי של expire_all_outside_tokens.
                if self._live_trading_ok():
                    try:
                        await self._live_close_outside_tokens(
                            (m.token_up, m.token_down), context=rollover_ctx
                        )
                    except Exception as e:
                        self.rt.log(f"סגירה אקטיבית: כשל כללי — {e}")
                settlement_trades = await self.demo.expire_all_outside_tokens(
                    (m.token_up, m.token_down),
                    context=rollover_ctx,
                )
                # FIX #3: כתיבה מיידית ל-history.db מתוך נתוני ה-settlement.
                # זה מבטל את ה-race עם auto_history_recorder_loop (10s):
                # FLW יקרא את היסטוריה מעודכנת מיד בכניסה הבאה.
                # אנחנו לוקחים את ה-resolved_outcome וה-btc prices ש-settlement חישב,
                # אז שני המקורות תמיד יסכימו. UPSERT (history_tracker fix #18)
                # ימנע התנגשות אם recorder כבר רשם משהו.
                try:
                    self._record_settlement_to_history(settlement_trades, rollover_ctx)
                except Exception as e:
                    self.rt.log(f"רישום היסטוריה: {e!r}")
                # אסוף token_ids שפורקו — לא להחזיר ב-reconcile (Data API עלול
                # עדיין להחזיר אותם → כפילות SETTLE + שחזור הפסד שגוי).
                settled_tids: set[str] = set()
                for st in settlement_trades:
                    tid = st.get("token_id") or ""
                    if tid:
                        settled_tids.add(tid)
                self.rt._settled_token_ids.update(settled_tids)
                if settled_tids:
                    self.rt._settled_token_ids_ts = time.time()
                if self._live_trading_ok():
                    await self._live_reconcile_if_enabled(
                        context=rollover_ctx,
                        force=True,
                        exclude_token_ids=self.rt._settled_token_ids,
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
            # FIX #22: גם persistence מתאפס ל-epoch החדש (לא להציל DCA ישן)
            self._persist_dca_counters()

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
        # m.order_min_size מגיע מ־Gamma ואז מעודכן מ־CLOB ‎/book‎ ב־discover (סמכותי למסחר)
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
        # ניקוי peak_state + tp_sell_cooldown מטוקנים שאין להם פוזיציה יותר (סוגר/רה-אנטרי)
        active_tokens_ps = {p.token_id for p in self.demo.state.positions}
        for tok_ps in list(self.rt._peak_state.keys()):
            if tok_ps not in active_tokens_ps:
                self.rt._peak_state.pop(tok_ps, None)
        for tok_cd in list(self.rt._tp_sell_cooldown_until.keys()):
            if tok_cd not in active_tokens_ps:
                self.rt._tp_sell_cooldown_until.pop(tok_cd, None)
        for p in list(self.demo.state.positions):
            b, _ = await fetch_best_bid_ask(p.token_id)
            if b is None:
                continue
            upnl = self.demo.unrealized_pnl_pct(p.token_id, b)
            try:
                b_cur = float(b)
            except (TypeError, ValueError):
                b_cur = 0.0
            # ── Hold-to-Resolution ────────────────────────────────────────────
            # אחרי N סלייסי DCA וכאשר bid ≥ סף ביטחון — לא מוציאים ב-TP.
            # מחזיקים עד סוף החלון (settle → $1.00) כדי לכסות הפסדי DCA קודמים.
            # חריג: בדקות האחרונות (freeze) תמיד מאפשרים יציאה רגילה אם יש רווח.
            # גם עוקפים את Peak Watchdog במצב זה (אחרת "נסיגה" תסגור מוקדם).
            hold_active = False
            hold_stop_trigger = False
            hold_stop_reason = ""
            if (
                cfg.hold_to_resolution_enabled
                and gate_ctx != "freeze"
                and self.rt.dca_done_slices >= max(0, int(cfg.hold_to_resolution_min_dca_slices))
                and b_cur >= float(cfg.hold_to_resolution_min_price)
            ):
                hold_active = True
                if cfg.hold_to_resolution_stop_loss_enabled:
                    try:
                        avg = float(p.avg_cost)
                    except Exception:
                        avg = 0.0
                    if avg > 0 and b_cur < avg:
                        hold_stop_trigger = True
                        hold_stop_reason = (
                            f"hold_stop_loss: bid {b_cur:.3f} < avg_cost {avg:.3f}"
                        )
            tp_allowed = tp_allowed_base or (
                dca_locked and upnl is not None and upnl >= cfg.dca_tp_override_pct
            )
            if hold_active:
                tp_allowed = False
            # ── Peak Watchdog ─────────────────────────────────────────────────
            # מעקב אחר שיא ה-bid ודגל "נגע ב-TP". אם נגע ואז נפל X% מהשיא → trigger יציאה.
            # מונע "פוזיציה שהייתה ברווח והפכה להפסד כי לא יצאנו ב-TP".
            peak_trigger = False
            peak_trigger_reason = ""
            try:
                tp_target_bid = float(p.avg_cost) * (
                    1.0 + (cfg.take_profit_pct / 100.0) * (1.0 + 2 * FEE_RATE)
                )
            except Exception:
                tp_target_bid = 0.0
            ps = self.rt._peak_state.setdefault(
                p.token_id,
                {"peak_bid": 0.0, "tp_touched": False, "tp_target_bid": tp_target_bid},
            )
            # רענון תקרת TP במקרה של DCA (avg_cost השתנה אחרי סלייס נוסף)
            ps["tp_target_bid"] = tp_target_bid
            try:
                b_val = float(b)
            except (TypeError, ValueError):
                b_val = 0.0
            if b_val > float(ps.get("peak_bid") or 0.0):
                ps["peak_bid"] = b_val
            if tp_target_bid > 0 and b_val >= tp_target_bid and not ps.get("tp_touched"):
                ps["tp_touched"] = True
                self.rt.log(
                    f"Peak Watchdog: {p.side} נגע ב-TP (bid {b_val:.3f} ≥ יעד {tp_target_bid:.3f}) — "
                    f"שומר על השיא; ירידה >{cfg.peak_retreat_exit_pct:.1f}% תפעיל יציאה מידית"
                )
            if (
                cfg.peak_watchdog_enabled
                and tp_allowed
                and ps.get("tp_touched")
                and float(ps.get("peak_bid") or 0.0) > 0
                and cfg.peak_retreat_exit_pct > 0
            ):
                retreat_trigger_bid = float(ps["peak_bid"]) * (
                    1.0 - cfg.peak_retreat_exit_pct / 100.0
                )
                if b_val <= retreat_trigger_bid:
                    peak_trigger = True
                    peak_trigger_reason = (
                        f"peak_retreat: bid {b_val:.3f} ≤ שיא {float(ps['peak_bid']):.3f} "
                        f"× (1-{cfg.peak_retreat_exit_pct:.1f}%)"
                    )
            # ──────────────────────────────────────────────────────────────────
            if upnl is not None and tp_allowed and cfg.near_tp_pct > 0:
                missing = cfg.take_profit_pct - upnl
                if 0 < missing <= cfg.near_tp_pct:
                    # מצטברים רק את הכי "קרוב" כדי לא להציף
                    label = p.side
                    if best_near_tp is None or missing < best_near_tp[0]:
                        best_near_tp = (missing, label)
            tp_trigger = (
                tp_allowed and upnl is not None and upnl >= cfg.take_profit_pct * (1 + 2 * FEE_RATE)
            )
            if tp_trigger or peak_trigger or hold_stop_trigger:
                tp_ctx = dict(base_ctx)
                if hold_stop_trigger:
                    tp_ctx["reason"] = f"HOLD_STOP {p.side}: {hold_stop_reason}"
                    self.rt.log_event(
                        f"Hold-to-Resolution stop-loss {p.side}: {hold_stop_reason}"
                    )
                elif peak_trigger and not tp_trigger:
                    tp_ctx["reason"] = f"PEAK_EXIT {p.side}: {peak_trigger_reason}"
                    self.rt.log_event(
                        f"Peak Watchdog יציאה {p.side}: {peak_trigger_reason} "
                        f"(upnl נוכחי {upnl:.2f}%)"
                    )
                else:
                    tp_ctx["reason"] = f"TP {p.side}: upnl={upnl:.2f}% >= {cfg.take_profit_pct:.2f}%"
                pos_contracts = float(p.contracts)
                if pos_contracts <= 1e-8:
                    continue
                if pos_contracts < float(m.order_min_size):
                    self.rt.log(
                        f"TP: פוזיציה {pos_contracts:.4f} חוזים — מתחת למינ׳ הזמנה {m.order_min_size} של השוק; "
                        f"מנסה סגירה (SELL) בכל זאת — אם הבורסה תדחה, סגר ידנית או הגדל/י מינ׳ חוזים בכניסה"
                    )
                if self._live_trading_ok():
                    cd_until = float(self.rt._tp_sell_cooldown_until.get(p.token_id) or 0.0)
                    now_ts = time.time()
                    if cd_until > now_ts:
                        # עיגול ל-ceil כדי שלא נציג "0s" בזמן שעדיין יש שבריר שנייה.
                        remaining_s = max(1, int(math.ceil(cd_until - now_ts)))
                        self.rt.status(
                            f"TP {p.side}: cooldown אחרי שגיאת יתרה — ממתין לסנכרון שרשרת "
                            f"({remaining_s}s)",
                            key=f"tp_cooldown_{p.token_id}",
                        )
                        continue
                    _, bid_tp = await fetch_best_bid_ask(p.token_id)
                    if bid_tp is None:
                        continue
                    lo = await live_clob.place_exit_order(
                        p.token_id,
                        pos_contracts,
                        float(bid_tp),
                        order_mode=getattr(cfg, "order_mode", "limit"),
                        exit_slippage_pct=float(getattr(cfg, "exit_slippage_pct", 5.0)),
                        retry_max_attempts=int(getattr(cfg, "retry_max_attempts", 3)),
                    )
                    if not lo.get("ok"):
                        self.rt.log(f"TP לייב: {lo.get('error', 'כשל')}")
                        if lo.get("error_code") == "insufficient_onchain_balance":
                            # יתרת שרשרת < ledger פנימי — מקור האמת היחיד הוא balance_allowance ב-CLOB,
                            # לא Data API (שמעוכב). סנכרן p.contracts ישירות לשרשרת; אם 0 — הסר פוזיציה.
                            chain_bal: Optional[float] = None
                            try:
                                chain_bal = await live_clob.fetch_chain_shares_for_token(p.token_id)
                            except Exception as e:
                                self.rt.log(f"TP לייב: שגיאת שליפת יתרת שרשרת — {e}")
                            if chain_bal is not None and chain_bal < 1e-4:
                                # אין מה למכור — הסר מהספר הפנימי לחלוטין (חוזי רפאים).
                                idx_rm = self.demo._position_idx(p.token_id)
                                if idx_rm >= 0:
                                    self.demo.state.positions.pop(idx_rm)
                                    self.demo.save()
                                self.rt._peak_state.pop(p.token_id, None)
                                self.rt._tp_sell_cooldown_until.pop(p.token_id, None)
                                self.rt.log_event(
                                    f"TP לייב: יתרת שרשרת ≈ 0 עבור {p.side} — הוסרה פוזיציה "
                                    f"פנימית מיותמת (מיל׳וי חלקי של GTC בכניסה)"
                                )
                            elif chain_bal is not None and chain_bal < float(p.contracts) - 1e-6:
                                # קצץ את הפוזיציה הפנימית לפי השרשרת — וקחו cooldown קצר
                                # כדי לא לנסות שוב מיד על אותו bid.
                                old_c = float(p.contracts)
                                p.contracts = float(chain_bal)
                                self.demo.save()
                                self.rt._tp_sell_cooldown_until[p.token_id] = now_ts + 3.0
                                self.rt.log(
                                    f"TP לייב: סנכרון פוזיציה {p.side} לפי שרשרת — "
                                    f"{old_c:.4f} → {chain_bal:.4f} חוזים (cooldown 3s)"
                                )
                            else:
                                # לא ידוע או זהה — נסיון reconcile רך + cooldown 15s.
                                self.rt._tp_sell_cooldown_until[p.token_id] = now_ts + 15.0
                                self.rt.log(
                                    "TP לייב: יתרת שרשרת < ledger — מפעיל reconcile מיידי + cooldown 15s"
                                )
                                try:
                                    await self._live_reconcile_if_enabled(
                                        context={"reason": "TP_BALANCE_ERROR"},
                                        force=True,
                                    )
                                except Exception as e:
                                    self.rt.log(f"reconcile מיידי נכשל: {e}")
                        continue
                    fill_sell = float(lo.get("fill_price") or lo.get("price") or bid_tp)
                    sold_sz = float(lo.get("size") or pos_contracts)
                    r = await self.demo.record_live_sell(
                        p.token_id,
                        fill_sell,
                        context=tp_ctx,
                        contracts_sold=sold_sz,
                    )
                else:
                    r = await self.demo.simulate_sell_all(p.token_id, context=tp_ctx)
                if r.get("ok"):
                    full_exit = bool(r.get("full_exit", True))
                    if full_exit:
                        self.rt.record_tp(cfg=cfg, side=p.side)
                        self.rt._peak_state.pop(p.token_id, None)
                        self.rt._tp_sell_cooldown_until.pop(p.token_id, None)
                        if cfg.loss_recovery_enabled:
                            self.demo.state.loss_recovery_streak = 0
                            self.demo.state.loss_recovery_multiplier = 1.0
                            self.demo.save()
                    tr = r.get("trade") or {}
                    price = tr.get("price")
                    peak = tr.get("peak_unrealized_pct")
                    trough = tr.get("trough_unrealized_pct")
                    sid = tr.get("session_id")
                    sold_c = float(tr.get("contracts") or 0.0)
                    if full_exit:
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
                    else:
                        self.rt.log_event(
                            f"TP לייב חלקי {p.side}: נמכרו ~{sold_c:.2f} חוזים @ "
                            f"{float(price) * 100:.1f}¢ — נשארה פוזיציה (יתרת CLOB < גודל ההזמנה המלא)"
                            if price is not None
                            else f"TP לייב חלקי {p.side}: נמכרו ~{sold_c:.2f} חוזים — נשארה פוזיציה",
                            session_id=sid,
                        )
                    if full_exit and (peak is not None or trough is not None):
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
                        lo = await live_clob.place_entry_order(
                            str(other_token),
                            float(n),
                            float(other_ask),
                            "BUY",
                            order_mode=getattr(cfg, "order_mode", "limit"),
                            entry_slippage_pct=float(getattr(cfg, "entry_slippage_pct", 2.0)),
                        )
                        if not lo.get("ok"):
                            self.rt.log(f"גידור אוטו (לייב): {lo.get('error', 'כשל')}")
                            return
                        fill_h = float(lo.get("fill_price") or lo.get("price") or other_ask)
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
            # יש פוזיציה פתוחה + DCA פעיל עם סלייסים שנותרו → ממשיכים DCA גם באזור ביניים
            has_open_position = pos_u or pos_d
            dca_has_remaining = cfg.dca_enabled and self.rt.dca_done_slices < cfg.dca_slices
            if has_open_position and dca_has_remaining:
                self.rt.status(
                    f"סטטוס: אזור ביניים ({min_left:.2f} דק׳) — ממשיך DCA ({self.rt.dca_done_slices}/{cfg.dca_slices})",
                    key="intermediate_dca_continue",
                    session_id=sid,
                )
                # לא עושים return — ממשיכים ללוגיקת הכניסה/DCA למטה
            else:
                if not has_open_position:
                    msg = f"סטטוס: אזור ביניים ({min_left:.2f} דק׳) — בלי כניסות חדשות (מוגדר)"
                else:
                    msg = f"סטטוס: אזור ביניים ({min_left:.2f} דק׳) — מנהל פוזיציה קיימת (אין כניסות חדשות)"
                self.rt.status(msg, key="intermediate", session_id=sid)
                return

        # Follow Last Winner (FLW): מעקף את side_preference אם פעיל ויש history.
        # אם DCA רץ ויש כבר פוזיציה — נוותר ונמשיך עם הצד הקיים (לא לקטוע סלייסים).
        flw_side: Optional[str] = None
        if (
            getattr(cfg, "follow_last_winner_enabled", False)
            and not (pos_u or pos_d)  # רק לעסקה חדשה, לא בתוך DCA רץ
        ):
            flw_side = self._resolve_follow_winner_side(cfg)
        if flw_side == "Up":
            if ask_u is None:
                self.rt.status("סטטוס: Ask חסר ל-Up (FLW) — לא ניתן להיכנס", key="book_missing_entry_up")
                return
            side, token, ask = "Up", token_up, ask_u
        elif flw_side == "Down":
            if ask_d is None:
                self.rt.status("סטטוס: Ask חסר ל-Down (FLW) — לא ניתן להיכנס", key="book_missing_entry_down")
                return
            side, token, ask = "Down", token_down, ask_d
        elif cfg.side_preference == "Down":
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
            # cap לכניסה הבאה: לא עוברים את המחיר הרצוי.
            # בדיקת DCA discount מבוססת על Bid (ירידה מול עלות הפוזיציה),
            # לא על Ask — כי ה-PnL שהמשתמש רואה מבוסס על Bid,
            # וב-spread רחב ה-Ask יכול להישאר גבוה גם כשה-Bid כבר ירד מספיק.
            entry_cap_price = price_usd

            # בדיקת DCA discount לפי Bid: אם הפוזיציה ירדה מספיק מול עלות הכניסה
            dca_bid_ok = True  # סלייס ראשון — תמיד מותר
            if self.rt.dca_done_slices > 0 and cfg.dca_discount_enabled and cfg.dca_discount_pct > 0:
                dca_bid_ok = False  # ברירת מחדל: חוסם עד שה-Bid יורד מספיק
                pos_idx = self.demo._position_idx(token)
                if pos_idx >= 0:
                    p_dca = self.demo.state.positions[pos_idx]
                    # שולפים bid עדכני לטוקן
                    bid_now, _ = await fetch_best_bid_ask(str(token))
                    if bid_now is not None and p_dca.avg_cost > 0:
                        drop_pct = (p_dca.avg_cost - float(bid_now)) / p_dca.avg_cost * 100.0
                        if drop_pct >= cfg.dca_discount_pct:
                            dca_bid_ok = True
                        else:
                            wp = (" · ".join(wait_parts) + " · ") if wait_parts else ""
                            self.rt.status(
                                f"סטטוס: {wp}DCA ממתין לירידה — Bid {float(bid_now):.2f}$ "
                                f"(ירידה {drop_pct:.1f}% מעלות {p_dca.avg_cost:.2f}$, "
                                f"נדרש {cfg.dca_discount_pct:.0f}%)",
                                key=f"dca_bid_wait:{token}:{self.rt.dca_done_slices}",
                                session_id=self.demo._session_by_token.get(token),
                                repeat_interval_sec=6.0,
                            )
                            return
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
            market_min = int(math.ceil(float(m.order_min_size)))
            if market_min > cfg.min_contracts:
                detail = (
                    f"מחושב {n}; מינ׳ השוק {market_min}, הגדרתך {cfg.min_contracts}"
                )
            else:
                detail = f"מחושב {n}"
            self.rt.status(
                f"סטטוס: סכום/מחיר לא מספיקים למינ׳ {min_c} חוזים ({detail})",
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
            lo = await live_clob.place_entry_order(
                str(token),
                float(n),
                float(lim),
                "BUY",
                order_mode=getattr(cfg, "order_mode", "limit"),
                entry_slippage_pct=float(getattr(cfg, "entry_slippage_pct", 2.0)),
            )
            if not lo.get("ok"):
                err = str(lo.get("error", "כשל"))
                t_fail = time.time()
                if (
                    err == self.rt._last_live_auto_entry_fail_msg
                    and t_fail - float(self.rt._last_live_auto_entry_fail_ts or 0) < 45.0
                ):
                    return
                self.rt._last_live_auto_entry_fail_msg = err
                self.rt._last_live_auto_entry_fail_ts = t_fail
                notional_check = float(lim) * float(n)
                lr_note = (
                    f"בסיס ${float(cfg.investment_usd):.2f} · מכפיל שחזור {lr_mult_snapshot:.2f}× "
                    f"· יעד אפקטיבי ${eff_inv_snapshot:.2f}"
                    if cfg.loss_recovery_enabled
                    else f"יעד השקעה (ללא שחזור) ${eff_inv_snapshot:.2f}"
                )
                dca_note = ""
                if cfg.dca_enabled:
                    ds = max(1, int(cfg.dca_slices))
                    per_slice = float(eff_inv_snapshot) / float(ds)
                    dca_note = (
                        f" · DCA סלייס {self.rt.dca_done_slices}/{cfg.dca_slices} · "
                        f"תקציב לסלייס ≈ ${per_slice:.2f} "
                        f"(יעד אפקטיבי ${eff_inv_snapshot:.2f} ÷ {ds})"
                    )
                self.rt.log(
                    f"כניסה אוטומטית (לייב) נכשלה: {err} — "
                    f"פירוט: {side} ×{float(n):.2f} חוזים · limit {float(lim):.4f}$ · "
                    f"בדיקת CLOB (מחיר×חוזים, ללא עמלה) ≈ ${notional_check:.2f} · "
                    f"עלות משוערת כולל עמלה ≈ ${planned_cost:.2f} · "
                    f"{lr_note}{dca_note} · "
                    f"Ask בשוק {_fmt_px(ask)} · cap כניסה {float(entry_cap_price):.4f}$"
                )
                return
            fill_a = float(lo.get("fill_price") or lo.get("price") or lim)
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
                lr_part = f" | שחזור הפסד: יעד אפקטיבי ${eff_inv_snapshot:.2f} (מכפיל {lr_mult_snapshot:.2f}× על בסיס {self._investment_base_label(cfg)})"
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
