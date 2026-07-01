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
# F5: גילוי שוק שמחזיר None — רושמים תקלה רק אם זה נמשך מעבר ל-60s, ולכל היותר פעם ב-120s.
DISCOVERY_NONE_PERSIST_SEC = 60.0
DISCOVERY_NONE_RECORD_INTERVAL_SEC = 120.0
_CB_COOLDOWN_SEC = 900.0  # 15 min — how long the circuit-breaker pauses new entries after a trip before auto-resuming
# ── תקרת-ברזל מוחלטת למכפיל שחזור-הפסד ────────────────────────────────────────────────
# 2026-06-15 INCIDENT: config ה-config סטה למצב מסוכן (loss_recovery_max_multiplier=100000)
# והמכפיל בסטייט טיפס ל-1525×, אז הבוט ניסה להיכנס ל-~$30k שוב ושוב ("insufficient balance")
# ורשם 274,840 תקלות שחנקו את ה-event loop. התקרה הזו היא בלם-ברזל בקוד: גם אם ה-config מתיר
# 100000 וה-state כבר 1525, גודל הפוזיציה לעולם לא יעבור base × HARD_MAX_LOSS_RECOVERY_MULT.
# חל גם על הצבירה (loss_recovery.py) — כך שהמכפיל המאוחסן עצמו לא יכול להצטבר מעבר לתקרה.
# בלם בטיחות בלבד: כשהמכפיל ≤ 3 או ששחזור-הפסד כבוי — אין שום שינוי התנהגות.
HARD_MAX_LOSS_RECOVERY_MULT = 3.0
# בלם יחסי-ליתרה: לעולם לא ננסה כניסה שה-notional שלה עולה על X מהיתרה הנוכחית של הדמו.
# מונע את הלולאה "לנסות להיכנס ל-$30k על יתרה של $7k בכל טיק" גם אם משהו אחר השתבש.
MAX_ENTRY_FRACTION_OF_BALANCE = 0.25


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
    # Risk limits (רך) — בלי SL, רק תקרות כדי למנוע ריצה לא מבוקרת.
    # ברירות מחדל שמרניות (incident 2026-06-15): שדות אלו לא נשמרו פעם, ובפרוד חזרו לערכי
    # ענק (50_000_000 / 100_000_000) שאיפשרו את לולאת ה-$30k. כעת הם בטוחים + נשמרים לדיסק.
    max_notional_per_window_usd: float = 1_000.0
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
    # ברירת מחדל = תקרת-הברזל (3.0). גם אם מישהו מעלה את ה-cap, ה-sizing נחסם ב-HARD_MAX_LOSS_RECOVERY_MULT.
    loss_recovery_max_multiplier: float = 3.0
    # ביצוע: "limit" (GTC קלאסי) או "market" (FOK לכניסה, FAK ליציאה + retry ladder).
    # מטרה: להבטיח ביצוע מידי ולמנוע פוזיציה תקועה כשהשוק מדלג על יעד ה-TP.
    order_mode: Literal["limit", "market"] = "limit"
    entry_slippage_pct: float = 2.0  # תקרת slippage לכניסת MARKET BUY
    exit_slippage_pct: float = 5.0   # תקרת slippage ליציאת MARKET SELL (רחבה — עדיף לצאת)
    # תקרת מחיר שפויה למצב market בלבד: לא נכנסים אם ה-Ask של הצד הנבחר עולה על
    # הסנטים האלה (מונע קניית "פייבוריט" יקר ב-0.85–0.95). 0 או 100 = ללא תקרה
    # (כניסה בכל מחיר). במצב limit אין השפעה — שם entry_price_cents הוא ה-cap.
    market_max_entry_price_cents: float = 80.0
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
    # Circuit-breaker — OPT-IN, OFF by default (no behavior change until enabled). A safety
    # brake that blocks NEW entries when a risk condition trips; open positions still exit.
    circuit_breaker_enabled: bool = False
    circuit_breaker_max_consecutive_losses: int = 0   # 0 = this condition off
    circuit_breaker_halt_at_cap: bool = False         # halt once loss-recovery multiplier hits its cap
    circuit_breaker_equity_floor_pct: float = 0.0     # 0 = off; halt if equity < this % of session baseline
    # Floor-stop (hard stop-loss): exit a losing position at this unrealized-loss %. OPT-IN.
    # 0 = off; e.g. 70 = exit when unrealized <= -70%. Always fires (it's a stop-loss), even
    # when take-profit is gated by DCA-lock / hold-to-resolution.
    floor_stop_pct: float = 0.0
    # Decision-mode — OPT-IN, DEFAULT "manual" (no behavior change). Controls WHO picks the
    # entry side. "manual": current behavior (FLW → side_preference → cheaper-ask). "suggest":
    # the SIGNAL picks the side and the entry is routed to pending_approval for the user to
    # approve. "auto": the SIGNAL picks the side and the bot enters automatically. In
    # suggest/auto a no-conviction signal (neutral / below decision_min_confidence) SKIPS the
    # window — it never falls back to cheaper-ask.
    decision_mode: Literal["manual", "suggest", "auto"] = "manual"
    decision_min_confidence: float = 60.0   # in suggest/auto, only act on signal recommendation at/above this confidence_pct


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
    circuit_breaker_tripped: bool = False
    circuit_breaker_reason: str = ""
    circuit_breaker_baseline_usd: Optional[float] = None  # session equity baseline (lazy-init)
    circuit_breaker_cooldown_until: float = 0.0
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
    # FLW: decision metadata של הבחירה האחרונה — להחתמה ב-trade context.
    # נקבע ב-_tick לפני הכניסה; מתאפס אחרי שימוש.
    _last_flw_decision: Optional[dict] = None
    # Peak Watchdog: {token_id: {"peak_bid": float, "tp_touched": bool, "tp_target": float}}
    # פוזיציה שעברה פעם את יעד ה-TP — אם ה-bid נופל אחורה, מוכרים מיד.
    _peak_state: dict = field(default_factory=dict)
    # Cooldown per-token על TP-SELL אחרי שגיאת "insufficient_onchain_balance":
    # מונע spam של 400-errors עד ש-reconcile יתקן את פער ה-ledger/chain.
    _tp_sell_cooldown_until: dict = field(default_factory=dict)
    # תשתית throttle לרישום תקלות (PR-A): מצמצמים שורות + כתיבות SQLite.
    _tp_fail_fault_ts: dict = field(default_factory=dict)          # F2: per-token, 60s
    _discovery_none_since: float = 0.0                              # F5
    _discovery_none_last_record_ts: float = 0.0                    # F5
    _reconcile_fail_streak: int = 0                                # F8
    _last_reconcile_fault_ts: float = 0.0                          # F8
    # Audit ledger: the last compute_signals() result (the rich "WHY" inputs).
    # A future signal-mode wiring populates this; until then it stays None and the
    # decision snapshot records signals_missing=true. Optional[dict] (JSON-safe).
    _last_signal_result: Optional[dict] = None
    _last_signal_refresh_ts: float = 0.0  # throttle for the audit signal refresh (>=15s apart)
    # Audit (recording-only): BTC spot captured at the START of the current window
    # (stamped at the rollover point from the cached signal's TA current_price). Lets a
    # future learner compute spot_vs_open_pct at entry. None until the first rollover.
    window_open_btc: Optional[float] = None
    # Audit (recording-only): the top-of-book snapshots the cached signal was computed
    # from (reused for raw_book_up/raw_book_down so we don't re-fetch). JSON-safe dicts.
    _last_signal_book_up: Optional[dict] = None
    _last_signal_book_down: Optional[dict] = None

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
        # Re-arm the circuit-breaker baseline to the post-reset equity (else the equity-floor
        # would measure against a stale pre-reset baseline and could trip spuriously).
        self.circuit_breaker_baseline_usd = None
        self.circuit_breaker_tripped = False
        self.circuit_breaker_reason = ""
        self.circuit_breaker_cooldown_until = 0.0

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


def sizing_price_per_contract(
    ask: Optional[float],
    entry_cap_usd: float,
    *,
    order_mode: str = "limit",
    entry_slippage_pct: float = 2.0,
) -> float:
    """מחיר ליחידה לחישוב כמות החוזים — חייב לשקף את מחיר ה-fill הצפוי כדי שההוצאה בפועל
    תהיה ≈ investment_usd.

    Bugfix (issue #2): במצב market ה-fill הוא ב-ask (×slippage, עד התקרה החוקית), *לא*
    ב-entry_cap_price. שימוש ב-min(cap, ask) כמו במצב limit גרם לכך שעם entry_price_cents=20
    ו-ask=51 חושבו ~24 חוזים אך ההוצאה בפועל הייתה ~פי 2.5 מ-investment_usd. לכן במצב market
    מתמחרים לפי entry_limit_price (אותו מחיר שאליו ה-order מתמלא). במצב limit — ללא שינוי.
    """
    if order_mode == "market" and ask is not None:
        try:
            a = float(ask)
        except (TypeError, ValueError):
            return effective_price_for_contract_qty(entry_cap_usd, ask)
        if math.isfinite(a) and MIN_LEGIT_SHARE_PRICE_USD <= a <= MAX_LEGIT_SHARE_PRICE_USD:
            return entry_limit_price(
                a, entry_cap_usd, order_mode="market", entry_slippage_pct=entry_slippage_pct
            )
    return effective_price_for_contract_qty(entry_cap_usd, ask)


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


def entry_limit_price(
    ask: float,
    entry_cap_price: float,
    *,
    order_mode: str = "limit",
    entry_slippage_pct: float = 2.0,
) -> float:
    """מחיר ה-limit הביצועי לכניסה (ה-price שנשלח ל-CLOB / לסימולציה).

    - order_mode="market" («כניסה בכל חלון»): מחיר marketable שמתמלא מיד —
      ‎ask × (1 + סליפג׳)‎, מוגבל רק לתקרת המחיר החוקית (0.99). ‎entry_cap_price‎
      משמש כאן רק לחישוב גודל הפוזיציה (ראה effective_price_for_contract_qty)
      ולא חוסם את הכניסה. כך הבוט נכנס לכל חלון 5/15 דק׳ כמו בפולימרקט גם
      כשהצד הנבחר (למשל ע"י FLW) יקר מ-entry_cap_price.
    - order_mode="limit" (משמעת מחיר): כמו קודם — ‎min(ask × 1.01, entry_cap_price)‎.
      לא משלמים מעל הסנטים שביקשת, אבל אם ה-Ask נשאר מעל ה-cap לכל אורך החלון
      ההזמנה לא תתמלא והחלון ידולג.
    """
    a = float(ask)
    if order_mode == "market":
        slip = max(0.0, float(entry_slippage_pct)) / 100.0
        return min(a * (1.0 + slip), MAX_LEGIT_SHARE_PRICE_USD)
    return min(a * 1.01, float(entry_cap_price))


def market_entry_price_too_high(
    ask: Optional[float],
    order_mode: str,
    market_max_entry_price_cents: float,
) -> bool:
    """האם לדלג על כניסה בגלל תקרת המחיר השפויה של מצב market?

    מחזיר True רק כאשר: order_mode="market", יש Ask תקין, התקרה בטווח (0,100),
    וה-Ask גבוה ממנה. ‎0 או ≥100 = ללא תקרה‎ (כניסה בכל מחיר). במצב limit תמיד
    False — שם entry_price_cents הוא ה-cap דרך entry_limit_price.
    """
    if order_mode != "market" or ask is None:
        return False
    try:
        cents = float(market_max_entry_price_cents)
        a = float(ask)
    except (TypeError, ValueError):
        return False
    if not (0.0 < cents < 100.0):
        return False
    return a > cents / 100.0


# B-4: לקוח httpx משותף עם keep-alive ל-fallback של ה-REST. ה-fallback רץ בתוך לולאת טיק של
# 0.12s כש-WS מתיישן — לקוח-לכל-קריאה משלם TLS handshake בכל פעם ומושך 429. ללא result cache:
# ה-WS כבר מקדים, ו-get_clob_book עדיין מושך חי בכל קריאה (מחיר ה-order נשאר טרי).
_BOOK_CLIENT: Optional[httpx.AsyncClient] = None


def _get_book_client() -> httpx.AsyncClient:
    global _BOOK_CLIENT
    if _BOOK_CLIENT is None:
        _BOOK_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=6.0, write=6.0, pool=6.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _BOOK_CLIENT


async def fetch_best_bid_ask(token_id: str) -> tuple[Optional[float], Optional[float]]:
    from ws_price_stream import price_stream
    bid, ask = price_stream.get_best_bid_ask(token_id)
    if bid is not None or ask is not None:
        tp = price_stream.get_price(token_id)
        if tp and (time.time() - tp.ts) < 30.0:
            return bid, ask
    try:
        book = await get_clob_book(_get_book_client(), token_id)
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
        # תקרת-ברזל מוחלטת: גם אם ה-state טיפס ל-1525× (incident 2026-06-15) או ה-config
        # מתיר 100000 — הגודל בפועל לעולם לא יעבור base × HARD_MAX_LOSS_RECOVERY_MULT.
        # כשהמכפיל ≤ התקרה אין שינוי התנהגות.
        m = min(m, HARD_MAX_LOSS_RECOVERY_MULT)
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
            # F9: תקלה אחת לכל rollover (לא בלולאת הטיק) על כשל סגירה אקטיבית. רק על sell_fail_msgs,
            # לא על no_bid (לזה יש backstop ב-expire). dedup_key קבוע.
            try:
                from fault_tracker import record_fault
                record_fault(
                    category="live_order", severity="medium",
                    title="סגירה אקטיבית של פוזיציות מחוץ לחלון נכשלה",
                    detail=" | ".join(sell_fail_msgs[:3])[:300],
                    source="strategy_runner._live_close_outside_tokens",
                    context={"fail_count": len(sell_fail_msgs)},
                    dedup_key="live_active_close_failed",
                )
            except Exception:
                pass
        return closed

    def _note_reconcile_fail(self, detail: str) -> None:
        """F8: רושם תקלה כש-reconfile לייב נכשל שוב ושוב. throttle עצמאי 120s (כי force=True
        עוקף את שער ה-reconcile), אחרי >=3 כשלים רצופים. UPSERT זעיר, dedup_key קבוע. לא זורק."""
        self.rt._reconcile_fail_streak += 1
        if self.rt._reconcile_fail_streak < 3:
            return
        _now = time.time()
        if (_now - float(self.rt._last_reconcile_fault_ts or 0)) < 120.0:
            return
        self.rt._last_reconcile_fault_ts = _now
        try:
            from fault_tracker import record_fault
            record_fault(
                category="reconcile", severity="high",
                title="סנכרון יתרה/פוזיציות לייב נכשל שוב ושוב",
                detail=str(detail)[:300],
                source="strategy_runner._live_reconcile_if_enabled",
                context={"streak": int(self.rt._reconcile_fail_streak)},
                dedup_key="live_reconcile_failed",
            )
        except Exception:
            pass

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
            self._note_reconcile_fail(str(e))  # F8
            return
        if not portfolio.get("ok"):
            self._note_reconcile_fail(str(portfolio.get("error") or "portfolio not ok"))  # F8
            return
        self.rt._reconcile_fail_streak = 0  # F8: fetch הצליח — מאפסים רצף כשלים
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
        # ── Circuit-breaker: SELF-RECOVERING. On a trip it pauses NEW entries for a cooldown,
        # records ONE fault, then auto-resumes (resetting the consecutive-loss streak) — so it
        # can never deadlock (the old bug: blocking entries meant no win could ever reset it).
        # Only the entry gate; never affects exits. Fail-OPEN. ──
        try:
            import circuit_breaker, time as _cbt
            _now = _cbt.time()
            if not getattr(cfg, "circuit_breaker_enabled", False):
                # Breaker OFF (the user's choice) → a genuine no-op at EVERY point, INCLUDING a
                # stale cooldown. Drop any in-flight trip/cooldown so unchecking the box frees new
                # entries on the very next tick. (The bug: the cooldown early-return below ran
                # before the enabled flag was ever read, so a disable was ignored for up to 15 min.)
                self.rt.circuit_breaker_cooldown_until = 0.0
                self.rt.circuit_breaker_tripped = False
                self.rt.circuit_breaker_reason = ""
            else:
                if _now < float(getattr(self.rt, "circuit_breaker_cooldown_until", 0.0) or 0.0):
                    # still cooling down — keep blocking, but do NOT re-evaluate or re-record (no spam)
                    self.rt.circuit_breaker_tripped = True
                    _left = int(float(self.rt.circuit_breaker_cooldown_until) - _now)
                    self.rt.status(f"🛑 Circuit-breaker: בקירור (עוד ~{_left//60+1} דק׳)", key="circuit_breaker")
                    return False
                # cooldown over (or never tripped): if we WERE tripped, clear it and give a CLEAN SLATE
                # so the consecutive-loss condition can't immediately re-trip (this breaks the deadlock).
                if getattr(self.rt, "circuit_breaker_tripped", False):
                    self.rt.circuit_breaker_tripped = False
                    self.rt.circuit_breaker_reason = ""
                    self.demo.state.loss_recovery_streak = 0
                # fresh evaluation
                _eq = None
                try:
                    _eq = float(self.demo.equity_snapshot_usd())
                except Exception:
                    _eq = None
                if self.rt.circuit_breaker_baseline_usd is None and _eq is not None:
                    self.rt.circuit_breaker_baseline_usd = _eq
                _cb_reason = circuit_breaker.should_halt(
                    enabled=True,
                    streak=int(self.demo.state.loss_recovery_streak or 0),
                    multiplier=float(self.demo.state.loss_recovery_multiplier or 1.0),
                    cap=float(getattr(cfg, "loss_recovery_max_multiplier", 10.0) or 10.0),
                    equity=_eq, baseline=self.rt.circuit_breaker_baseline_usd,
                    max_consecutive_losses=int(getattr(cfg, "circuit_breaker_max_consecutive_losses", 0) or 0),
                    halt_at_cap=bool(getattr(cfg, "circuit_breaker_halt_at_cap", False)),
                    equity_floor_pct=float(getattr(cfg, "circuit_breaker_equity_floor_pct", 0.0) or 0.0),
                )
                if _cb_reason:
                    # NEW trip → start the cooldown + record the fault ONCE (not every tick)
                    self.rt.circuit_breaker_tripped = True
                    self.rt.circuit_breaker_reason = _cb_reason
                    self.rt.circuit_breaker_cooldown_until = _now + _CB_COOLDOWN_SEC
                    try:
                        from fault_tracker import record_fault
                        record_fault(category="risk", severity="critical",
                                     title="Circuit-breaker עצר כניסות חדשות (בקירור אוטומטי)",
                                     detail=f"{_cb_reason} — קירור ~{int(_CB_COOLDOWN_SEC//60)} דק׳ ואז חידוש אוטומטי",
                                     source="strategy_runner._entry_limits_ok",
                                     context={"reason": _cb_reason}, dedup_key="circuit_breaker_tripped")
                    except Exception:
                        pass
                    self.rt.status(f"🛑 Circuit-breaker: {_cb_reason} — קירור ~{int(_CB_COOLDOWN_SEC//60)} דק׳", key="circuit_breaker")
                    return False
                self.rt.circuit_breaker_tripped = False
                self.rt.circuit_breaker_reason = ""
        except Exception as _e:
            print(f"[circuit_breaker] eval failed (non-fatal, fail-open): {_e!r}", flush=True)
        # ── end circuit-breaker ──
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
        # ── בלם יחסי-ליתרה (incident 2026-06-15) ──
        # גם אם משהו אחר השתבש (config מסוכן, מכפיל מנופח), לעולם לא ננסה כניסה שה-notional
        # שלה עולה על MAX_ENTRY_FRACTION_OF_BALANCE מהיתרה — מונע את הלולאה "לנסות $30k על $7k
        # בכל טיק" ("insufficient balance" שוב ושוב). תקלה אחת מדודדפת בלבד, לא אחת לכל טיק.
        try:
            _bal = float(self.demo.state.balance_usd)
        except Exception:
            _bal = 0.0
        if (
            _bal > 0
            and planned_cost_usd > 0
            and planned_cost_usd > _bal * MAX_ENTRY_FRACTION_OF_BALANCE
        ):
            self.rt.status(
                f"סטטוס: כניסה (~${planned_cost_usd:.2f}) חורגת מ-{MAX_ENTRY_FRACTION_OF_BALANCE * 100:.0f}% מהיתרה "
                f"(${_bal:.2f}) — נחסם להגנה",
                key="limit_entry_fraction_of_balance",
            )
            try:
                from fault_tracker import record_fault
                record_fault(
                    category="risk", severity="high",
                    title="כניסה נחסמה: notional גדול מדי ביחס ליתרה",
                    detail=(
                        f"notional ~${planned_cost_usd:.2f} > {MAX_ENTRY_FRACTION_OF_BALANCE * 100:.0f}% "
                        f"מיתרה ${_bal:.2f} — נחסם (הגנה מפני לולאת insufficient-balance)"
                    ),
                    source="strategy_runner._entry_limits_ok",
                    context={"planned_cost_usd": round(planned_cost_usd, 2),
                             "balance_usd": round(_bal, 2),
                             "max_fraction": MAX_ENTRY_FRACTION_OF_BALANCE},
                    dedup_key="entry_notional_exceeds_balance_fraction",
                )
            except Exception:
                pass
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
            # עקביות עם נתיב ה-rollover הראשי: גם כש-mode=off נכתב ל-history.db.
            # כך אם המשתמש מדליק auto אחרי כן, FLW יקבל את הנתון העדכני מיד.
            try:
                self._record_settlement_to_history(settlement_trades, rollover_ctx)
            except Exception as e:
                self.rt.log(f"רישום היסטוריה (off): {e!r}")

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
            # F5: גילוי נכשל לגמרי (גם ה-stale-cache נעלם). רושמים תקלה רק אם זה נמשך >60s,
            # ולכל היותר פעם ב-120s — UPSERT זעיר, dedup_key קבוע (שורה אחת). לעולם לא זורק.
            try:
                _now = time.time()
                if self.rt._discovery_none_since == 0.0:
                    self.rt._discovery_none_since = _now
                _persisted = _now - self.rt._discovery_none_since
                if (_persisted > DISCOVERY_NONE_PERSIST_SEC
                        and _now - self.rt._discovery_none_last_record_ts > DISCOVERY_NONE_RECORD_INTERVAL_SEC):
                    from fault_tracker import record_fault
                    record_fault(
                        category="discovery", severity="high",
                        title="אין שוק BTC Up/Down פעיל — גילוי נכשל שוב ושוב",
                        detail=f"window={self.rt.config.btc_window} discovery=None for {int(_persisted)}s (stale cache gone too)",
                        source="strategy_runner._tick",
                        context={"window": self.rt.config.btc_window, "persisted_sec": int(_persisted)},
                        dedup_key="discovery_none_persistent",
                    )
                    self.rt._discovery_none_last_record_ts = _now
            except Exception:
                pass
            return
        # F5: גילוי הצליח — מאפסים את מד ה-None.
        if self.rt._discovery_none_since != 0.0:
            self.rt._discovery_none_since = 0.0
        if m.epoch != self.rt.current_epoch:
            # הגנה מפני "הבהוב" בגילוי (epoch קופץ קדימה/אחורה תחת עומס API): מבצעים
            # rollover רק כשזו התקדמות אמיתית — epoch גדול יותר וגם החלון הנוכחי
            # הסתיים בפועל לפי שעון. אחרת מתעלמים מה-epoch שהתגלה (לא מתחשבנים, לא
            # מאפסים מצב-חלון, לא נכנסים מחדש) — מונע ריבוי כניסות/הפסדים באותו חלון.
            try:
                from market_discovery import window_step_sec as _wss
                _ws_cur = int(_wss(self.rt.config.btc_window))
            except Exception:
                _ws_cur = 300
            _cur = int(self.rt.current_epoch or 0)
            if _cur != 0 and (m.epoch < _cur or time.time() < (_cur + _ws_cur)):
                self.rt.status(
                    f"גילוי לא יציב: התקבל חלון {m.epoch} בעוד החלון הנוכחי {_cur} עדיין פעיל — "
                    f"מתעלמים (מונע התחשבנות/כניסה מוקדמת באותו חלון)",
                    key="discovery_flap_ignored",
                    repeat_interval_sec=20.0,
                )
                try:
                    from fault_tracker import record_fault
                    record_fault(
                        category="discovery", severity="medium",
                        title="גילוי חלון לא יציב (flap) — חלון התעלם",
                        detail=f"discovered epoch={m.epoch} while current={_cur} still active",
                        source="strategy_runner._tick.rollover",
                        context={"discovered": m.epoch, "current": _cur},
                        dedup_key="discovery_flap",
                    )
                except Exception:
                    pass
                return
            # FIX #24 (v2): lock + sentinel pattern.
            # ה-v1 הראשון רק עטף את ה-double-check וזה לא הספיק — בין שחרור ה-lock
            # לבין עדכון current_epoch ב-end של ה-rollover (שורה ~1087), קריאה
            # מקבילה הייתה יכולה להיכנס שוב. עכשיו: בתוך ה-lock אנחנו מעדכנים
            # IMMEDIATELY את current_epoch ל-m.epoch (סנטינל). כל tick אחר יראה
            # epoch == current_epoch וייצא בדלת. את ה-epoch הישן שומרים ב-local
            # variable כדי להעביר ל-rollover_ctx ולשאר הפעולות.
            async with self._rollover_lock:
                if m.epoch == self.rt.current_epoch:
                    # rollover כבר בוצע בקריאה אחרת. דלג.
                    return
                prev_epoch = self.rt.current_epoch
                # סנטינל: כל קריאה מקבילה ל-_tick תראה equal ותחזור.
                self.rt.current_epoch = m.epoch
            # לפני מעבר חלון: פירוק פוזיציות מחלון קודם (SETTLE_WIN / SETTLE_LOSS / …)
            settlement_trades: list[dict[str, Any]] = []
            if prev_epoch != 0:
                from market_discovery import window_step_sec

                rollover_ctx = {
                    "settled_epoch": prev_epoch,
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
                    # תוצאה לא-ידועה (SETTLE_UNKNOWN / settlement_error) אינה הפסד אמיתי
                    # ואסור שתיחשב כ-has_loss (זה הזין את שחזור-ההפסד בתקלת ה-incident).
                    has_loss = any(
                        float(t.get("realized_pnl") or 0) < 0
                        and str(t.get("type") or "") != "SETTLE_UNKNOWN"
                        and not t.get("settlement_error")
                        for t in settlement_trades
                    )
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
                        # F10: התראה כשמכפיל שחזור-ההפסד מטפס גבוה (כסף אמיתי מסלים). תצפית בלבד —
                        # קורא ערכים שכבר חושבו, פעם בחלון תחת ה-rollover lock, dedup_key קבוע. לא save נוסף.
                        try:
                            from fault_tracker import record_fault
                            _m_now = float(self.demo.state.loss_recovery_multiplier or 1.0)
                            _cap = float(cfg_lr.loss_recovery_max_multiplier or 10.0)
                            if _m_now >= max(3.0, _cap * 0.8):
                                record_fault(
                                    category="risk", severity="high",
                                    title="מכפיל שחזור-הפסד גבוה",
                                    detail=f"multiplier={_m_now:.2f}x streak={int(self.demo.state.loss_recovery_streak)} (תקרה {_cap:.1f}x)",
                                    source="strategy_runner._tick.loss_recovery",
                                    context={"multiplier": round(_m_now, 2),
                                             "streak": int(self.demo.state.loss_recovery_streak), "cap": round(_cap, 2)},
                                    dedup_key="loss_recovery_high_multiplier",
                                )
                        except Exception:
                            pass
                        for line in lr_lines:
                            self.rt.log_event(line)
                    else:
                        # Loss-recovery is OFF — but still maintain loss_recovery_streak as a faithful
                        # consecutive-loss counter (win→0, loss→+1, skip UNKNOWN/error exactly like
                        # apply_loss_recovery) so the circuit-breaker's "halt after N losses" works
                        # without loss-recovery. The MULTIPLIER is left untouched (stays 1.0).
                        _streak_changed = False
                        for t in settlement_trades:
                            if str(t.get("type") or "") == "SETTLE_UNKNOWN" or t.get("settlement_error"):
                                continue
                            rp = t.get("realized_pnl")
                            if rp is None:
                                continue
                            try:
                                r = float(rp)
                            except (TypeError, ValueError):
                                continue
                            if r > 0 and self.demo.state.loss_recovery_streak != 0:
                                self.demo.state.loss_recovery_streak = 0
                                _streak_changed = True
                            elif r < 0:
                                self.demo.state.loss_recovery_streak += 1
                                _streak_changed = True
                        if _streak_changed:
                            self.demo.save()
                        if has_loss:
                            self.rt.log_event(
                                "שחזור הפסד: כבוי בהגדרות המנוע — פירוק עם הפסד לא מגדיל מכפיל ולא משנה סכום לסלייס. "
                                "הפעל «שחזור אחרי הפסד», לחץ שמור הגדרות, והפעל מחדש את המנוע אם צריך לטעון config מהדיסק."
                            )
            self.rt.log(f"מעבר חלון → {m.slug}")
            # current_epoch כבר עודכן בתוך ה-lock כסנטינל — לא לעדכן שוב.
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
            # ── Audit (recording-only): capture BTC spot at the START of the new window. ──
            # Taken from the latest cached signal's TA current_price (in-memory, NO network).
            # A future learner uses it for spot_vs_open_pct at entry. Best-effort: on any
            # miss leave it None — must never disturb the rollover / trade.
            try:
                _ta_open = ((self.rt._last_signal_result or {}).get("sub", {}).get("ta", {}) or {})
                _open_px = _ta_open.get("current_price")
                self.rt.window_open_btc = float(_open_px) if _open_px is not None else None
            except Exception:
                self.rt.window_open_btc = None
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

        # ── Audit: refresh the cached signal snapshot for the decision "WHY". ──
        # Cheap: compute_signals() makes NO extra network fetches. We now feed CLOB book
        # DEPTH straight from the in-process WS stream (already on the wire, free) so the
        # 0.30-weight CLOB-imbalance sub-signal is finally populated in the AUDIT ledger.
        # DATA-ONLY: the bot still chooses its side by cheaper-ask, NOT this signal.
        # Best-effort — must never block a trade. Throttled to >=15s, bounded by wait_for so
        # a cache-miss Binance fetch can never stall a tick. Stamp the throttle ts BEFORE
        # awaiting to avoid retry storms. Passing books bypasses compute_signals' 30s no-books
        # cache, but the >=15s throttle bounds recompute and the analysis is in-memory.
        # If get_book() returns None (no fresh depth) we pass None -> clob stays unavailable
        # this tick (graceful); a stale book is dropped to None, never fed as bad data.
        try:
            _now_sig = time.time()
            if _now_sig - getattr(self.rt, "_last_signal_refresh_ts", 0.0) >= 15.0:
                self.rt._last_signal_refresh_ts = _now_sig
                from signal_engine import compute_signals as _compute_signals
                _up_book = _down_book = None
                try:
                    from ws_price_stream import price_stream as _price_stream
                    _up_book = _price_stream.get_book(token_up, max_age_sec=30.0)
                    _down_book = _price_stream.get_book(token_down, max_age_sec=30.0)
                except Exception:
                    _up_book = _down_book = None
                # Audit (recording-only): stash the SAME book snapshots the signal was
                # computed from so the audit_inputs raw_book_* reuses them (no re-fetch).
                self.rt._last_signal_book_up = _up_book
                self.rt._last_signal_book_down = _down_book
                self.rt._last_signal_result = await asyncio.wait_for(
                    _compute_signals(
                        up_book=_up_book, down_book=_down_book,
                        window_sec=int(m.window_sec)),
                    timeout=1.5)
        except Exception as _e:
            print(f"[audit] signal refresh skipped (non-fatal): {_e!r}", flush=True)

        # ── Audit ledger: stash the point-in-time decision inputs (the "WHY"). ──
        # Plain dict (JSON-safe). The demo_engine BUY hook completes the snapshot with the
        # final side + execution. Best-effort: must never block a trade.
        try:
            import audit_snapshot
            _sig_result = getattr(self.rt, "_last_signal_result", None)
            _ta = (_sig_result or {}).get("sub", {}).get("ta", {}) or {}
            # vol_bucket from the cached signal's ATR as a % of price (reuses in-memory data,
            # no network). Cheap recording-only context for a future learner.
            _atr = _ta.get("atr")
            _px = _ta.get("current_price")
            if _atr is not None and _px is not None and float(_px) > 0:
                _atr_pct = float(_atr) / float(_px) * 100.0
                _vol_bucket = "low" if _atr_pct < 0.03 else "high" if _atr_pct > 0.08 else "mid"
            else:
                _vol_bucket = None
            # BTC spot at entry, taken from the same cached signal TA (can be ~30s stale — an
            # approximate spot is far more useful to a learner than NULL).
            _btc_spot_at_entry = _ta.get("current_price")

            # ── PART 2 (recording-only): prediction-market features. ─────────────────
            # The Polymarket share asks ARE the market's implied probabilities. The edge
            # (model vs market) MUST be captured at decision time — it cannot be rebuilt
            # later. None-safe via clob_imbalance.market_features. Does NOT affect trade.
            _market_feats = None
            try:
                from clob_imbalance import market_features as _market_features
                _model_up_prob = (_sig_result or {}).get("up_confidence") or 0.0
                _market_feats = _market_features(
                    up_ask=ask_u, down_ask=ask_d, model_up_prob=_model_up_prob)
            except Exception as _mfe:
                print(f"[audit] market_features skipped (non-fatal): {_mfe!r}", flush=True)
                _market_feats = None

            # ── PART 3 (recording-only): RAW capture (lossless future-proofing). ─────
            # raw_book_up/down: full top-10-level snapshot REUSED from the cached signal's
            # books (no re-fetch) as compact [[price,size],...]. funding_rate_pct: the
            # funding-rate value (rate_pct, a PERCENT) from sub.sentiment.funding. window_open_btc +
            # spot_vs_open_pct: the BTC spot at window start vs now. All None-safe.
            def _compact_book(_bk, _n=10):
                try:
                    if not _bk:
                        return None
                    _bids = [[float(x.get("price", 0)), float(x.get("size", 0))]
                             for x in (_bk.get("bids") or [])[:_n]]
                    _asks = [[float(x.get("price", 0)), float(x.get("size", 0))]
                             for x in (_bk.get("asks") or [])[:_n]]
                    return {"bids": _bids, "asks": _asks}
                except Exception:
                    return None
            _raw_book_up = _compact_book(getattr(self.rt, "_last_signal_book_up", None))
            _raw_book_down = _compact_book(getattr(self.rt, "_last_signal_book_down", None))
            try:
                _funding = (_sig_result or {}).get("sub", {}).get("sentiment", {}).get("funding", {}) or {}
                _funding_rate_pct = _funding.get("rate_pct")
            except Exception:
                _funding_rate_pct = None
            _window_open_btc = getattr(self.rt, "window_open_btc", None)
            try:
                if (_window_open_btc is not None and float(_window_open_btc) != 0
                        and _btc_spot_at_entry is not None):
                    _spot_vs_open_pct = (
                        (float(_btc_spot_at_entry) - float(_window_open_btc))
                        / float(_window_open_btc) * 100.0
                    )
                else:
                    _spot_vs_open_pct = None
            except Exception:
                _spot_vs_open_pct = None

            base_ctx["audit_inputs"] = {
                "mode": ("live" if getattr(self.rt, "live_trading", False) else "demo"),
                "slug": m.slug, "epoch": int(m.epoch), "window_sec": int(m.window_sec),
                "code_version": (audit_snapshot.get_git_sha() or "")[:12],
                "signal_result": _sig_result,
                "btc_spot_at_entry": _btc_spot_at_entry,
                "policy": {
                    "order_mode": getattr(cfg, "order_mode", None),
                    "take_profit_pct": getattr(cfg, "take_profit_pct", None),
                    "entry_price_cents_cap": getattr(cfg, "entry_price_cents", None),
                    "side_preference": getattr(cfg, "side_preference", None),
                    "loss_recovery_enabled": getattr(cfg, "loss_recovery_enabled", None),
                    "loss_recovery_multiplier": self.demo.state.loss_recovery_multiplier,
                    "loss_recovery_streak": self.demo.state.loss_recovery_streak,
                },
                "book": {"ask_u": ask_u, "bid_u": bid_u, "ask_d": ask_d, "bid_d": bid_d},
                "provenance": {"book_source": "ws", "signals_missing": _sig_result is None,
                               "btc_spot_stale": True, "entry_logic": "cheaper_ask"},
                "regime": {"vol_bucket": _vol_bucket, "seconds_remaining_at_entry": sec_left,
                           "entry_minute_in_window": int((int(m.window_sec) - sec_left) // 60)},
                # PART 2 — prediction-market features (recording-only; edge captured at
                # decision time). None if asks/model-prob unavailable.
                "market": _market_feats,
                # PART 3 — RAW capture (recording-only, lossless future-proofing).
                "raw_book_up": _raw_book_up,
                "raw_book_down": _raw_book_down,
                "funding_rate_pct": _funding_rate_pct,
                "window_open_btc": _window_open_btc,
                "spot_vs_open_pct": _spot_vs_open_pct,
            }
        except Exception as _e:
            print(f"[audit] audit_inputs build failed (non-fatal): {_e!r}", flush=True)

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
        for tok_ff in list(self.rt._tp_fail_fault_ts.keys()):  # F2: prune fault-throttle dict
            if tok_ff not in active_tokens_ps:
                self.rt._tp_fail_fault_ts.pop(tok_ff, None)
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
            # ── Floor-stop (hard stop-loss) ───────────────────────────────────
            # OPT-IN absolute loss floor. Bypasses tp_allowed / DCA-lock / hold-to-
            # resolution gating on purpose: a stop-loss must always be able to fire.
            floor_trigger = (
                float(getattr(cfg, "floor_stop_pct", 0.0) or 0.0) > 0.0
                and upnl is not None
                and upnl <= -float(cfg.floor_stop_pct)
            )
            if tp_trigger or peak_trigger or hold_stop_trigger or floor_trigger:
                tp_ctx = dict(base_ctx)
                if floor_trigger:
                    tp_ctx["reason"] = f"FLOOR_STOP {p.side}: upnl {upnl:.1f}% <= -{float(cfg.floor_stop_pct):.0f}%"
                    self.rt.log(f"Floor-stop יציאה {p.side}: הפסד {upnl:.1f}% הגיע לרצפה -{float(cfg.floor_stop_pct):.0f}% — יוצאים")
                elif hold_stop_trigger:
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
                        # F2: רישום תקלה על כשל יציאת TP בלייב (כסף אמיתי / phantom shares).
                        # throttle per-token 60s; dedup_key per-error-code; UPSERT זעיר. לעולם לא זורק.
                        try:
                            _now_f = time.time()
                            if _now_f - float(self.rt._tp_fail_fault_ts.get(p.token_id) or 0.0) >= 60.0:
                                self.rt._tp_fail_fault_ts[p.token_id] = _now_f
                                _code = str(lo.get("error_code") or "tp_exit_failed")
                                from fault_tracker import record_fault
                                record_fault(
                                    category="live_order", severity="medium",
                                    title="יציאת TP בלייב נכשלה — " + _code,
                                    detail=str(lo.get("error"))[:300],
                                    source="strategy_runner.tp.live",
                                    context={"token_id": p.token_id, "side": p.side,
                                             "contracts": float(p.contracts), "error_code": _code},
                                    dedup_key="tp_exit_failed:" + _code,
                                )
                        except Exception:
                            pass
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
                            _exit_rp = (r.get("trade") or {}).get("realized_pnl")
                            if _exit_rp is not None and float(_exit_rp) >= 0:
                                self.demo.state.loss_recovery_streak = 0
                                self.demo.state.loss_recovery_multiplier = 1.0
                            else:
                                # a loss-exit (floor-stop / peak-retreat into loss) counts
                                # as a loss, not a win — keep building the recovery streak.
                                self.demo.state.loss_recovery_streak += 1
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

        # ── Decision-mode: let the SIGNAL pick the side (opt-in). Default "manual" skips this
        # entirely (cheaper-ask below). In suggest/auto, only act on a confident recommendation;
        # otherwise SKIP the window (don't trade a no-conviction signal). ──
        _sig_decided_side = None
        _sig_rec = ""
        _sig_conf = 0.0
        if cfg.decision_mode != "manual" and not (pos_u or pos_d):
            _sig = self.rt._last_signal_result or {}
            _sig_rec = (_sig.get("recommendation") or "").strip()
            _sig_conf = float(_sig.get("confidence_pct") or 0.0)
            if _sig_rec in ("Up", "Down") and _sig_conf >= float(getattr(cfg, "decision_min_confidence", 60.0) or 60.0):
                _sig_decided_side = _sig_rec
            else:
                self.rt.status(
                    f"מצב-החלטה {cfg.decision_mode}: הסיגנל לא מספיק בטוח ({_sig_rec or 'neutral'} {_sig_conf:.0f}%) — מדלגים על החלון",
                    key="decision_mode_skip")
                return

        # Follow Last Winner (FLW): מעקף את side_preference אם פעיל ויש history.
        # אם DCA רץ ויש כבר פוזיציה — נוותר ונמשיך עם הצד הקיים (לא לקטוע סלייסים).
        flw_side: Optional[str] = None
        if (
            getattr(cfg, "follow_last_winner_enabled", False)
            and not (pos_u or pos_d)  # רק לעסקה חדשה, לא בתוך DCA רץ
        ):
            flw_side = self._resolve_follow_winner_side(cfg)
            if flw_side is not None:
                # Observability: לוג חיובי כש-FLW בחר. נחתם בעמדת ה-trade context מאוחר יותר.
                self.rt.status(
                    f"FLW: עוקב אחרי המנצח → {flw_side} "
                    f"(lookback={int(getattr(cfg, 'follow_last_winner_lookback', 1) or 1)}, "
                    f"mode={getattr(cfg, 'follow_last_winner_mode', 'forward')})",
                    key="flw_active",
                    repeat_interval_sec=30.0,
                )
                # סימון לרישום ב-trade context שעסקה זו נבחרה ע"י FLW.
                self.rt._last_flw_decision = {
                    "side": flw_side,
                    "lookback": int(getattr(cfg, "follow_last_winner_lookback", 1) or 1),
                    "mode": str(getattr(cfg, "follow_last_winner_mode", "forward")),
                    "ts": time.time(),
                }
        if _sig_decided_side == "Up":
            if ask_u is None:
                self.rt.status("סטטוס: Ask חסר ל-Up (מצב-החלטה) — לא ניתן להיכנס", key="book_missing_entry_up")
                return
            side, token, ask = "Up", token_up, ask_u
        elif _sig_decided_side == "Down":
            if ask_d is None:
                self.rt.status("סטטוס: Ask חסר ל-Down (מצב-החלטה) — לא ניתן להיכנס", key="book_missing_entry_down")
                return
            side, token, ask = "Down", token_down, ask_d
        elif flw_side == "Up":
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

        # תקרת מחיר שפויה למצב market בלבד: אם הצד הנבחר יקר מהתקרה — מדלגים על
        # החלון (לא קונים "פייבוריט" יקר). 0 או ≥100 = ללא תקרה (כניסה בכל מחיר).
        # במצב limit אין השפעה — שם entry_price_cents הוא ה-cap.
        if market_entry_price_too_high(
            ask,
            getattr(cfg, "order_mode", "limit"),
            getattr(cfg, "market_max_entry_price_cents", 0.0),
        ):
            mmax_cents = float(getattr(cfg, "market_max_entry_price_cents", 0.0) or 0.0)
            self.rt.status(
                f"סטטוס: צד {side} יקר מדי ({_fmt_px(ask)} > תקרת market {mmax_cents:.0f}¢) — מדלג על החלון",
                key="market_price_cap_skip",
                session_id=self.demo._session_by_token.get(token),
                repeat_interval_sec=10.0,
            )
            return

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
            qty_px = sizing_price_per_contract(
                ask, entry_cap_price,
                order_mode=getattr(cfg, "order_mode", "limit"),
                entry_slippage_pct=getattr(cfg, "entry_slippage_pct", 2.0),
            )
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
            qty_px = sizing_price_per_contract(
                ask, price_usd,
                order_mode=getattr(cfg, "order_mode", "limit"),
                entry_slippage_pct=getattr(cfg, "entry_slippage_pct", 2.0),
            )
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
        if ask > entry_cap_price and getattr(cfg, "order_mode", "limit") != "market":
            # מצב limit בלבד: ה-Ask מעל ה-cap → נצמיד limit ל-entry_cap_price (עלול לא להתמלא).
            # במצב market אין cap חוסם — נכנסים מיד, אז לא מציגים הודעת "ממתין".
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

        # ה-limit: במצב "limit" לא עובר את הסנטים שביקשת (או פחות), וב-DCA מקיים
        # drop קשיח. במצב "market" — מחיר marketable שמתמלא מיד (ask × (1+סליפג׳)),
        # בלי חסימה ל-entry_cap_price, כדי שהבוט ייכנס לכל חלון 5/15 דק׳ כמו פולימרקט
        # גם כשהצד הנבחר יקר מה-cap (entry_cap_price נשאר רק לחישוב גודל הפוזיציה).
        lim = entry_limit_price(
            ask,
            entry_cap_price,
            order_mode=getattr(cfg, "order_mode", "limit"),
            entry_slippage_pct=float(getattr(cfg, "entry_slippage_pct", 2.0)),
        )
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
        # Always stamp the effective investment for the audit ledger (it was previously set
        # only when loss-recovery was on, leaving it None on every row when martingale is off).
        # Recording-only — does not affect sizing (n was already computed above).
        entry_ctx["effective_investment_usd"] = eff_inv_snapshot
        if cfg.loss_recovery_enabled:
            entry_ctx["loss_recovery_multiplier"] = lr_mult_snapshot
        # FLW: חתימה ב-trade context אם הכניסה נבחרה ע"י FLW. ה-UI/אנליטיקס יוכלו
        # לסנן או להציג "כניסה זו הגיעה מ-FLW: side=X, lookback=N, mode=Y".
        flw_dec = getattr(self.rt, "_last_flw_decision", None)
        if isinstance(flw_dec, dict) and flw_dec.get("side") == side:
            entry_ctx["flw_chosen"] = True
            entry_ctx["flw_lookback"] = flw_dec.get("lookback")
            entry_ctx["flw_mode"] = flw_dec.get("mode")
            # מאפסים אחרי שימוש כדי לא לסמן שוב אם כניסה זו נדחית/חוזרת
            self.rt._last_flw_decision = None
        # Decision-mode: record WHY the side was chosen by the signal (audit ledger).
        if _sig_decided_side is not None:
            entry_ctx["decision_mode"] = cfg.decision_mode
            entry_ctx["decision_signal_rec"] = _sig_rec
            entry_ctx["decision_signal_confidence_pct"] = _sig_conf
        # Suggest decision-mode routes the entry through the EXISTING pending_approval gate
        # (the same one semi-mode uses), so the user approves/rejects each signal-driven entry
        # via the /pending UI — REGARDLESS of the run mode. "auto" decision-mode still enters
        # automatically unless the run mode is itself "semi".
        wants_approval = (mode == "semi") or (cfg.decision_mode == "suggest" and _sig_decided_side is not None)
        if wants_approval:
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
                # F1: רישום תקלה על כשל כניסת לייב (נתיב כסף אמיתי). מצומצם ע"י ה-dedup של 45s
                # למעלה + dedup_key מקבוצה סופית → לכל היותר ~6 שורות. UPSERT זעיר ל-faults.db.
                try:
                    from fault_tracker import record_fault
                    code = str(lo.get("error_code") or "").strip()
                    if not code:
                        low = err.lower()
                        if "signature" in low or "funder" in low:
                            code = "signature_or_funder"
                        elif "py-clob-client" in low or "חסר" in err:
                            code = "clob_client_missing"
                        else:
                            code = "live_entry_failed"
                    record_fault(
                        category="live_order",
                        severity="high" if code in ("insufficient_onchain_balance", "signature_or_funder") else "medium",
                        title="כניסת לייב נכשלה — " + code,
                        detail=err[:300], source="strategy_runner.auto_entry.live",
                        context={"side": side, "contracts": float(n), "limit": float(lim), "error_code": code},
                        dedup_key="live_entry_failed:" + code,
                    )
                except Exception:
                    pass
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
        else:
            # שקיפות: כשל קנייה בדמו (יתרה לא מספקת / Ask מעל הלימיט) לא נבלע יותר —
            # נרשם ביומן וב-לשונית התקלות. זה מה שהמשתמש ראה כ"אין מספיק כסף".
            err = str(r.get("error") or "כשל לא ידוע")
            self.rt.log(
                f"כניסה אוטומטית (דמו) נכשלה: {err} — {side} ×{float(n):.2f} @ limit {float(lim):.4f}$ · "
                f"יעד אפקטיבי ${eff_inv_snapshot:.2f} (מכפיל {lr_mult_snapshot:.2f}×) · "
                f"יתרה ${self.demo.state.balance_usd:.2f}"
            )
            try:
                from fault_tracker import record_fault
                insufficient = "יתרה" in err
                record_fault(
                    category="entry_failed",
                    severity="high" if insufficient else "medium",
                    title="כניסה בדמו נכשלה — יתרה לא מספקת" if insufficient else "כניסה בדמו נכשלה",
                    detail=f"{err} | {side} ×{float(n):.2f} @ {float(lim):.4f}$",
                    source="strategy_runner.demo_auto_entry",
                    context={"side": side, "contracts": float(n), "limit": float(lim),
                             "balance": round(float(self.demo.state.balance_usd), 2),
                             "eff_inv": round(float(eff_inv_snapshot), 2)},
                    dedup_key="demo_entry_failed:insufficient" if insufficient else "demo_entry_failed",
                )
            except Exception:
                pass
