"""
מנוע דמו: יתרה וירטואלית, פוזיציות, מילוי מול ספר פקודות אמיתי.
עמלה: ~0.2% לצד (maker/taker 1000 ב-basis points של Polymarket — נלקח 0.2% כברירת מחדל).
"""
from __future__ import annotations

import json
import asyncio
import math
import os
import time
import uuid
import csv
import io
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from atomic_io import atomic_write_json
from order_validation import validate_contracts_for_market
from pricing_limits import MAX_LEGIT_SHARE_PRICE_USD, MIN_LEGIT_SHARE_PRICE_USD

FEE_RATE = 0.002  # 0.2% לצד כהערכה
# PR-D: השהיית persist (כתיבת 6MB) מחוץ ל-event-loop + throttle ל-backfill הכבד.
PERSIST_INTERVAL_SEC = 20.0   # לכל היותר כתיבת state אחת כל 20s מנתיב הקריאה (fire-and-forget)
BACKFILL_THROTTLE_SEC = 30.0  # ה-backfill הכבד (רשת + סריקה) רץ לכל היותר כל 30s, לא בכל poll

Side = Literal["Up", "Down"]

# לקוח HTTP משותף ל־mark_to_market (במקום AsyncClient חדש בכל קריאה — חוסך TLS והמתנה)
_demo_clob_httpx: Optional[httpx.AsyncClient] = None


def _get_demo_clob_httpx() -> httpx.AsyncClient:
    global _demo_clob_httpx
    if _demo_clob_httpx is None:
        _demo_clob_httpx = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=8.0),
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )
    return _demo_clob_httpx


@dataclass
class Position:
    side: Side
    contracts: float
    avg_cost: float
    token_id: str
    window_epoch: Optional[int] = None
    window_sec: Optional[int] = None


@dataclass
class DemoState:
    balance_usd: float = 10_000.0
    positions: list[Position] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    equity_history: list[tuple[float, float]] = field(default_factory=list)
    last_mark: dict = field(default_factory=dict)
    trade_seq: int = 0  # מספר סידורי — עולה בכל session חדש, נשמר לדיסק
    # שחזור אחרי הפסד (StrategyRunner) — נשמר בדמו
    loss_recovery_streak: int = 0
    loss_recovery_multiplier: float = 1.0
    # מסמן «סשן» אחרי איפוס לוח/סטטיסטיקה — רק עסקאות מ-ts זה והלאה נכללות בתצוגה ובמיזוג חי ל-v3
    stats_epoch_ts: Optional[float] = None
    # FIX #22: DCA counters persisted לדיסק — שורדים restart של השרת.
    # קודם היו רק ב-StrategyRuntime (זיכרון), והבוט "שכח" איפה היה ב-DCA אחרי קריסה,
    # מה שיכל לגרום לכפילות slice (לקנות פעמיים את אותה רמת מחיר).
    dca_done_slices_persisted: int = 0
    dca_last_dca_ts_persisted: float = 0.0
    dca_last_fill_price_persisted: Optional[float] = None
    dca_active_epoch_persisted: int = 0  # ה-epoch של החלון שבו נמצא ה-DCA

    def next_trade_num(self) -> int:
        """מחזיר מספר מחזור הבא (מונוטוני, לא מתאפס)."""
        self.trade_seq += 1
        return self.trade_seq

    def to_dict(self) -> dict:
        return {
            "balance_usd": self.balance_usd,
            "positions": [asdict(p) for p in self.positions],
            "trades": self.trades[-50_000:],  # ~12,500 מחזורים (4 עסקאות בממוצע למחזור)
            "equity_history": self.equity_history[-5000:],
            "last_mark": self.last_mark,
            "trade_seq": self.trade_seq,
            "loss_recovery_streak": self.loss_recovery_streak,
            "loss_recovery_multiplier": self.loss_recovery_multiplier,
            "stats_epoch_ts": self.stats_epoch_ts,
            # FIX #22: DCA counters persisted
            "dca_done_slices_persisted": self.dca_done_slices_persisted,
            "dca_last_dca_ts_persisted": self.dca_last_dca_ts_persisted,
            "dca_last_fill_price_persisted": self.dca_last_fill_price_persisted,
            "dca_active_epoch_persisted": self.dca_active_epoch_persisted,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DemoState":
        _raw_se = d.get("stats_epoch_ts")
        _se: Optional[float] = None
        if _raw_se is not None and _raw_se != "":
            try:
                _se = float(_raw_se)
            except (TypeError, ValueError):
                _se = None
        # FIX #22: DCA counters from disk
        dca_lp = d.get("dca_last_fill_price_persisted")
        dca_lp_val: Optional[float] = None
        if dca_lp is not None:
            try:
                dca_lp_val = float(dca_lp)
            except (TypeError, ValueError):
                dca_lp_val = None
        st = cls(
            balance_usd=float(d.get("balance_usd", 10_000)),
            trades=list(d.get("trades") or []),
            equity_history=[tuple(x) for x in (d.get("equity_history") or [])],
            trade_seq=int(d.get("trade_seq", 0)),
            loss_recovery_streak=int(d.get("loss_recovery_streak", 0) or 0),
            loss_recovery_multiplier=float(d.get("loss_recovery_multiplier", 1.0) or 1.0),
            stats_epoch_ts=_se,
            dca_done_slices_persisted=int(d.get("dca_done_slices_persisted", 0) or 0),
            dca_last_dca_ts_persisted=float(d.get("dca_last_dca_ts_persisted", 0.0) or 0.0),
            dca_last_fill_price_persisted=dca_lp_val,
            dca_active_epoch_persisted=int(d.get("dca_active_epoch_persisted", 0) or 0),
        )
        # שחזור trade_seq מה-trades עצמם אם חסר (תאימות אחורה)
        if st.trade_seq == 0 and st.trades:
            max_num = max((t.get("trade_num") or 0) for t in st.trades)
            st.trade_seq = max_num
        for p in d.get("positions") or []:
            we = p.get("window_epoch")
            ws = p.get("window_sec")
            st.positions.append(
                Position(
                    side=p["side"],
                    contracts=float(p["contracts"]),
                    avg_cost=float(p["avg_cost"]),
                    token_id=p["token_id"],
                    window_epoch=int(we) if we is not None else None,
                    window_sec=int(ws) if ws is not None else None,
                )
            )
        return st


# מעקב שיא/שפל + מסלול רווח/הפסד לכל פוזיציה (בזיכרון, לא נשמר).
# מפתח: token_id. מתנקה כשפוזיציה נסגרת.
# דגימות מסלול: תואם לרענון UI (~1s) + זנב חי בצד הלקוח. לא לדגום בערך כל 3s — הגרף נראה "מקוטע".
POSITION_TRACKING_PATH_INTERVAL = 1.0  # שניות בין דגימות (מינימום בין נקודות שמורות בשרת)
POSITION_TRACKING_PATH_MIN_DELTA_PCT = 0.12  # דגום גם כשהשינוי >= X% (תנועות קטנות עדיין נרשמות עם האינטרוול)
# קפיצה חדה (למשל +30% ל־-20% בין דגימות) — תמיד לרשום דגימה נוספת
POSITION_TRACKING_PATH_FORCE_DELTA_PCT = 0.45
# גלילה: נשמרות ה־N האחרונות (לא מפסיקים לדגום אחרי N).
# לא משנה את מהירות הדגימה — רק אורך ההיסטוריה בגרף.
# ברירת מחדל הועלתה ל-5000: מספיק ל-15 דק׳ בקצב volatile מלא (5/שנ' × 900 = 4500),
# כך שב-99% מהמקרים אין trim — המשתמש רואה את כל ההיסטוריה כולל peaks מוקדמים.
# כש-cap כן נחרג (חלון 30 דק׳, volatility אקסטרים) — _smart_trim_path משמר את הכניסה,
# peak, trough והדגימות האחרונות, ודוגם מהאמצע אחיד.
def _position_tracking_path_max() -> int:
    default = 5000
    raw = os.environ.get("POSITION_TRACKING_PATH_MAX")
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip(), 10)
        return max(64, min(v, 50_000))
    except ValueError:
        return default


POSITION_TRACKING_PATH_MAX = _position_tracking_path_max()


def _smart_trim_path(path: list, max_len: int, peak_ts: Optional[float], trough_ts: Optional[float]) -> list:
    """מקצץ את ה-path בצורה חכמה: שומר entry / peak / trough / recent, ומדגם מהאמצע אחיד.

    קריטריוני שמירה (לפי סדר עדיפות):
    1. הדגימה הראשונה (נקודת כניסה).
    2. הדגימה האחרונה (תמיד עדכנית).
    3. דגימה תואמת ל-high_watermark_ts (peak).
    4. דגימה תואמת ל-low_watermark_ts (trough).
    5. min(100, max_len/4) הדגימות האחרונות (פעילות עדכנית).
    6. דגימה אחידה (step uniform) מתוך הנותרות.

    O(n) בזיכרון ובזמן. מחזיר רשימה חדשה.
    """
    n = len(path)
    if n <= max_len:
        return path
    must_keep: set[int] = {0, n - 1}
    # peak / trough — מזהים לפי ts בדיוק של 1ms
    if peak_ts is not None:
        for i, s in enumerate(path):
            sts = s.get("ts")
            if isinstance(sts, (int, float)) and abs(float(sts) - float(peak_ts)) < 0.001:
                must_keep.add(i)
                break
    if trough_ts is not None:
        for i, s in enumerate(path):
            sts = s.get("ts")
            if isinstance(sts, (int, float)) and abs(float(sts) - float(trough_ts)) < 0.001:
                must_keep.add(i)
                break
    # שומרים את N הדגימות האחרונות לפעילות עדכנית בגרף
    recent_n = min(100, max(1, max_len // 4))
    for i in range(max(0, n - recent_n), n):
        must_keep.add(i)
    # דגימה אחידה מהנותרות
    remaining_target = max_len - len(must_keep)
    if remaining_target > 0:
        candidates = [i for i in range(n) if i not in must_keep]
        if candidates:
            step = max(1.0, len(candidates) / remaining_target)
            for k in range(remaining_target):
                idx = int(k * step)
                if idx < len(candidates):
                    must_keep.add(candidates[idx])
    return [path[i] for i in sorted(must_keep)]


def _settled_pnl_path_max() -> int:
    """PR-G: כמה נקודות pnl_path שומרים על עסקה שנסגרה (היסטורית). ברירת מחדל 50 —
    מספיק לגרף חלק בכרטיס המקופל, וקטן פי ~12 מ-~600 הנקודות הגולמיות שכל חלון ייצר."""
    default = 50
    raw = os.environ.get("SETTLED_PNL_PATH_MAX")
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(8, min(int(str(raw).strip(), 10), 1000))
    except ValueError:
        return default


SETTLED_PNL_PATH_MAX = _settled_pnl_path_max()


def _trim_settled_path(tr: dict) -> list:
    """מקצץ את pnl_path של עסקה שנסגרת ל-SETTLED_PNL_PATH_MAX נקודות (משמר כניסה/peak/
    trough/אחרונות, ראה _smart_trim_path). זה השורש לכל נפיחות ה-state: כל פירוק שמר את
    כל ~600 דגימות החלון × ~150B ⇒ state 26MB ⇒ json.dumps ~2s שחנק את ה-event-loop
    (תקלת event_loop_lag). הגרף ההיסטורי בכרטיס נשאר חלק כי נשמרים entry/peak/trough/last."""
    return _smart_trim_path(
        list(tr.get("path") or []),
        SETTLED_PNL_PATH_MAX,
        tr.get("high_watermark_ts"),
        tr.get("low_watermark_ts"),
    )


def _settlement_pnl_if_held(trade: dict[str, Any]) -> Optional[float]:
    """Recording-only counterfactual for the audit ledger: what the position would have netted
    if held to the $1/$0 binary resolution (payoff − stake), regardless of how it actually
    exited. Pure arithmetic from the trade dict. Returns None when the window didn't resolve
    Up/Down (e.g. a TP/stop early exit, where resolved_outcome is absent) or leg_cost is unknown.
    """
    resolved_outcome = trade.get("resolved_outcome")
    leg_cost = trade.get("leg_cost")
    side = trade.get("side")
    contracts = trade.get("contracts")
    if resolved_outcome not in ("Up", "Down") or leg_cost is None or contracts is None:
        return None
    try:
        payoff = float(contracts) if (resolved_outcome and side == resolved_outcome) else 0.0
        return payoff - float(leg_cost)
    except (TypeError, ValueError):
        return None


# תנודתיות גבוהה בין טיקי mark_to_market מלאים → חלון דגימה אגרסיבית (יותר קריאות CLOB + צפוף path)
VOLATILE_TICK_DELTA_PCT = 4.0
VOLATILE_WINDOW_SEC = 14.0
MARK_THROTTLE_VOLATILE_SEC = 0.22
PATH_INTERVAL_VOLATILE_SEC = 0.32


class DemoEngine:
    def __init__(self, state_path: Optional[Path] = None):
        if state_path is not None:
            self.state_path = state_path
        else:
            env_state_path = os.environ.get("DEMO_STATE_PATH")
            if env_state_path:
                self.state_path = Path(env_state_path)
            else:
                data_root = os.environ.get("DATA_ROOT")
                if data_root:
                    self.state_path = Path(data_root) / "demo_state.json"
                else:
                    self.state_path = Path(__file__).parent / "demo_state.json"
        self.state = self._load()
        self._position_tracking: dict[str, dict[str, Any]] = {}
        self._post_exit_tracking: dict[str, dict[str, Any]] = {}  # token_id -> מעקב שיא/שפל פוטנציאלי אחרי TP
        self._session_by_token: dict[str, str] = {}
        self._mark_aggressive_until: float = 0.0  # mark_to_market מהיר יותר אחרי קפיצה גדולה בין טיקים
        # PR-D: persist מושהה מחוץ ל-event-loop + throttle ל-backfill, כדי ש-/api/demo/state
        # לא ישמור 6MB סינכרונית בכל poll ולא יריץ את ה-backfill הכבד בכל poll (גרם ל-6s).
        self._dirty = False
        self._persist_in_flight = False
        self._last_persist_ts = 0.0
        self._last_backfill_ts = 0.0
        self._backfill_session_ids()
        self._compact_oversized_pnl_paths()

    def _compact_oversized_pnl_paths(self) -> None:
        """PR-G: דחיסה חד-פעמית של ה-backlog — עסקאות שנסגרו עם pnl_path ענק (מלפני התיקון)
        מקצצות ל-SETTLED_PNL_PATH_MAX. בלי זה ה-state נשאר 26MB לנצח (עסקה שנסגרה לא נכתבת
        מחדש). רץ פעם אחת בטעינה; ה-persist הבא כותב state קטן ⇒ event_loop_lag נעלם."""
        changed = 0
        for t in self.state.trades:
            p = t.get("pnl_path")
            if isinstance(p, list) and len(p) > SETTLED_PNL_PATH_MAX:
                t["pnl_path"] = _smart_trim_path(
                    p, SETTLED_PNL_PATH_MAX, t.get("peak_ts"), t.get("trough_ts")
                )
                changed += 1
        if changed:
            self._dirty = True
            print(f"[demo] compacted pnl_path on {changed} settled trades (state slimmed)", flush=True)

    def _load(self) -> DemoState:
        if self.state_path.exists():
            try:
                return DemoState.from_dict(json.loads(self.state_path.read_text()))
            except Exception:
                pass
        return DemoState()

    def _backfill_session_ids(self) -> None:
        """ממלא session_id לעסקאות ישנות לפי token_id — session_id = id של ה-BUY הראשון."""
        token_to_session: dict[str, str] = {}
        for t in sorted(self.state.trades, key=lambda x: float(x.get("ts") or 0)):
            tid = t.get("token_id")
            if not tid:
                continue
            ttype = t.get("type") or ""
            sid = t.get("session_id")
            if ttype == "BUY":
                if tid not in token_to_session:
                    sid = sid or t.get("id")
                    if sid:
                        token_to_session[tid] = sid
                        t["session_id"] = sid
                else:
                    sid = token_to_session[tid]
                    if not t.get("session_id"):
                        t["session_id"] = sid
                continue
            if ttype in ("SELL_TP", "EXPIRE_0", "SETTLE_WIN", "SETTLE_LOSS", "SETTLE_UNKNOWN") or (
                ttype and "SELL" in str(ttype)
            ):
                if not sid:
                    sid = token_to_session.pop(tid, None)
                    if sid:
                        t["session_id"] = sid
                else:
                    token_to_session.pop(tid, None)
                continue
            if not sid and tid in token_to_session:
                t["session_id"] = token_to_session[tid]
        self._session_by_token = {p.token_id: token_to_session[p.token_id] for p in self.state.positions if p.token_id in token_to_session}

    def equity_snapshot_usd(self) -> float:
        """הערכת שווי נטו ל-baseline (מעדיף last_mark.equity; אחרת יתרה במזומן)."""
        lm = self.state.last_mark or {}
        eq = lm.get("equity")
        if isinstance(eq, (int, float)) and math.isfinite(eq) and eq >= 0:
            return float(eq)
        return float(self.state.balance_usd)

    def _equity_marked_consistent(self) -> float:
        """equity נקודתי בזמן אירוע מסחר — עקבי עם mark_to_market.

        הנוסחה במארק היא ``balance + sum(bid * contracts * (1-FEE_RATE))`` עם ה-bid האחרון
        של כל leg. חישוב חלופי לפי ``avg_cost`` (ללא הוספת עמלת יציאה) יוצר פער קבוע
        כלפי מעלה אל מול נקודות mark, ומכאן spikes מדומים בגרף Run P&L. כאן בוחרים את
        ה-bid מ-``last_mark.legs`` אם יש; לפוזיציה טרייה שעדיין לא מסומנת משתמשים ב-avg_cost
        (הכי טוב שיש) עם אותו פקטור ``(1-FEE_RATE)`` כדי להישאר באותו "קנה־מידה" כמו מארק.
        """
        lm = self.state.last_mark or {}
        legs_by_tid: dict[str, dict] = {}
        for leg in lm.get("legs") or []:
            tid = leg.get("token_id") if isinstance(leg, dict) else None
            if isinstance(tid, str):
                legs_by_tid[tid] = leg
        value = 0.0
        for p in self.state.positions:
            leg = legs_by_tid.get(p.token_id)
            bid: Optional[float] = None
            if leg is not None:
                b = leg.get("mark_bid")
                if isinstance(b, (int, float)) and math.isfinite(float(b)) and float(b) > 0:
                    bid = float(b)
            price = bid if bid is not None else float(p.avg_cost)
            value += price * float(p.contracts) * (1.0 - FEE_RATE)
        return float(self.state.balance_usd) + value

    def save(self) -> None:
        # PR-G: indent=None (compact) — קובץ ה-state נקרא ע"י מכונה (json.loads), אז indent=2
        # רק הכפיל את הגודל (~26MB→48MB) ואת זמן ה-json.dumps שמחזיק את ה-GIL.
        atomic_write_json(self.state_path, self.state.to_dict(), indent=None)

    def reset(self, balance: float = 10_000.0) -> None:
        """איפוס חשבון: יתרה חדשה, בלי פוזיציות, בלי סימוני מחיר — עסקאות ישנות נשמרות לדיסק (ניתוח v3)."""
        preserved_trades = list(self.state.trades)
        preserved_seq = int(self.state.trade_seq)
        now = time.time()
        self.state = DemoState(
            balance_usd=balance,
            positions=[],
            trades=preserved_trades,
            equity_history=[],
            last_mark={},
            trade_seq=preserved_seq,
            loss_recovery_streak=0,
            loss_recovery_multiplier=1.0,
            stats_epoch_ts=now,
        )
        self._position_tracking.clear()
        self._post_exit_tracking.clear()
        self._session_by_token.clear()
        self._backfill_session_ids()
        self.save()

    def clear_stats(self) -> None:
        """איפוס תצוגת סטטיסטיקה בלבד — לא מוחק עסקאות (נשמרות ל-v3)."""
        self._post_exit_tracking.clear()
        self.state.stats_epoch_ts = time.time()
        self.state.equity_history = []
        self.state.last_mark = {}
        self.save()

    async def reset_stats_and_flatten_positions(self) -> None:
        """סגירת פוזיציות פתוחות + התחלת «סשן» סטטיסטיקה חדש בלי למחוק עסקאות מהיסטוריה."""
        # 1) נקה גרף equity בזיכרון; עסקאות נשארות ב-state לדיסק / ניתוח v3
        self.state.equity_history = []

        # 2) Flatten פוזיציות: נמיר אותן ליתרה לפי best bid (עם עמלה בקירוב)
        async with httpx.AsyncClient() as client:
            for p in list(self.state.positions):
                try:
                    r = await client.get(
                        "https://clob.polymarket.com/book",
                        params={"token_id": p.token_id},
                        timeout=15.0,
                    )
                    if r.status_code != 200:
                        continue
                    bids = list((r.json().get("bids") or []))
                    if not bids:
                        continue
                    try:
                        bids.sort(key=lambda x: float(x["price"]), reverse=True)
                        bid = float(bids[0]["price"])
                    except Exception:
                        continue
                except Exception:
                    continue

                proceeds = bid * p.contracts * (1 - FEE_RATE)
                self.state.balance_usd += proceeds

        self.state.positions = []
        self._position_tracking.clear()
        self._post_exit_tracking.clear()
        self._session_by_token.clear()
        self.state.stats_epoch_ts = time.time()
        self.state.last_mark = {
            "equity": self.state.balance_usd,
            "unrealized_usd": 0.0,
            "ts": time.time(),
            "legs": [],
        }
        self.save()

    async def expire_all_outside_tokens(
        self,
        valid_tokens: tuple[str, str],
        context: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """פירוק פוזיציות מחלון קודם — לפי תוצאת Up/Down (סוף מול תחילה, פרוקסי Binance).

        מחזיר את רשימת רשומות העסקה שנוספו (בסדר עיבוד) — לעדכון loss recovery וכו׳.
        """
        from btc_price import fetch_window_start_end_btc_usd

        ctx = dict(context or {})
        settled_epoch_ctx = ctx.get("settled_epoch")
        if settled_epoch_ctx is None:
            settled_epoch_ctx = ctx.get("epoch")
        settled_ws_raw = ctx.get("settled_window_sec")
        if settled_ws_raw is None:
            settled_ws_raw = ctx.get("window_sec")
        default_ws = int(settled_ws_raw) if settled_ws_raw is not None else 300

        now_ts = time.time()
        keep: list[Position] = []
        to_settle: list[Position] = []
        for p in list(self.state.positions):
            if p.token_id in valid_tokens:
                keep.append(p)
            elif (
                p.window_epoch is not None
                and now_ts < (int(p.window_epoch) + int(p.window_sec or default_ws))
            ):
                # הגנה מפני "הבהוב" בגילוי: הטוקן אמנם לא בחלון שהתגלה כרגע, אבל
                # החלון של הפוזיציה עדיין לא הסתיים בפועל (לפי שעון). מחזיקים עד
                # סוף החלון — לא מתחשבנים מוקדם. זה מונע ריבוי הפסדים באותו חלון.
                keep.append(p)
            else:
                to_settle.append(p)

        price_cache: dict[tuple[int, int], dict[str, Any]] = {}

        async def _prices_for(ep: int, ws: int) -> dict[str, Any]:
            """FIX #26: לא לשמור ב-cache ערך עם None — אם kline עדיין לא זמין
            (קריאה מיד אחרי סגירת חלון), הקריאה הבאה תנסה שוב במקום להחזיר None.
            """
            key = (ep, ws)
            cached = price_cache.get(key)
            if cached is not None and cached.get("start") is not None and cached.get("end") is not None:
                return cached
            fresh = await fetch_window_start_end_btc_usd(ep, ws)
            # רק אם שני המחירים תקפים — שמור ב-cache
            if fresh.get("start") is not None and fresh.get("end") is not None:
                price_cache[key] = fresh
            return fresh

        created: list[dict[str, Any]] = []
        for p in to_settle:
            leg_cost = p.avg_cost * p.contracts * (1 + FEE_RATE)
            ws = int(p.window_sec or default_ws)
            ep: Optional[int] = p.window_epoch
            if ep is None and settled_epoch_ctx is not None:
                ep = int(settled_epoch_ctx)

            sid = self._session_by_token.pop(p.token_id, None)
            tr = self._position_tracking.pop(p.token_id, None)

            trade: dict[str, Any] = {
                "id": str(uuid.uuid4())[:8],
                "ts": time.time(),
                "side": p.side,
                "contracts": p.contracts,
                "token_id": p.token_id,
                "tp_hit": False,
                "settled_window_sec": ws,
            }
            trade["leg_cost"] = leg_cost
            if sid:
                trade["session_id"] = sid
            else:
                # פוזיציה שנטענה מה-chain דרך reconcile — לא נפתחה ב-BUY של הריצה הזו.
                # מסמנים כדי שה-UI לא יציג אותה כ«עסקה» ריקה ללא צד/משך, ושהסטטיסטיקה
                # של הריצה לא תכלול אותה ב-win_rate (היתרה עצמה מתעדכנת רגיל).
                trade["reconcile_origin"] = True
            if tr:
                trade["peak_unrealized_pct"] = tr.get("high_watermark_pct")
                trade["peak_ts"] = tr.get("high_watermark_ts")
                trade["peak_mark_bid"] = tr.get("high_mark_bid")
                trade["trough_unrealized_pct"] = tr.get("low_watermark_pct")
                trade["trough_ts"] = tr.get("low_watermark_ts")
                trade["trough_mark_bid"] = tr.get("low_mark_bid")
                trade["pnl_path"] = _trim_settled_path(tr)
                trade["open_ts"] = tr.get("open_ts")

            trade.update(ctx)
            if ep is not None:
                trade["settled_epoch"] = ep

            if ep is None:
                # תוצאה לא-ידועה (חסר epoch). לא מענישים על כשל פנימי: מבטלים את
                # העסקה ומחזירים את הסטייק (realized_pnl=None ⇒ לא מזין שחזור-הפסד).
                trade["type"] = "SETTLE_UNKNOWN"
                trade["price"] = 0.0
                trade["fee_est"] = 0.0
                trade["realized_pnl"] = None
                trade["voided"] = True
                trade["settlement_error"] = "missing_window_epoch"
                trade["settlement_condition"] = "לא ניתן לחשב — חסר epoch לחלון (בוטל, הסטייק הוחזר)"
                self.state.balance_usd += leg_cost
                try:
                    from fault_tracker import record_fault
                    record_fault(
                        category="settlement", severity="high",
                        title="התחשבנות לא ודאית — חסר epoch לחלון",
                        detail=f"side={p.side} ×{p.contracts} — בוטל, הוחזרו ${leg_cost:.2f}",
                        source="demo_engine.expire_all_outside_tokens",
                        context={"token_id": p.token_id, "side": p.side, "refund": round(leg_cost, 2)},
                        dedup_key="settle_unknown:missing_window_epoch",
                    )
                except Exception:
                    pass
                self.state.trades.append(trade)
                self._audit_finalize_settle_trade(trade)
                created.append(trade)
                continue

            px = await _prices_for(ep, ws)
            start_p = px.get("start")
            end_p = px.get("end")
            trade["settlement_btc_start"] = start_p
            trade["settlement_btc_end"] = end_p
            trade["settlement_price_source"] = px.get("source", "binance_1m_proxy")
            trade["settlement_condition"] = "BTC בסוף החלון ≥ BTC בתחילת החלון ⇒ Up (פרוקסי Binance, לא Chainlink)"

            if start_p is None or end_p is None:
                # מחיר BTC לא זמין בעת ההתחשבנות (כשל זמני ב-Binance proxy סביב
                # סגירת החלון). זו התקלה שניפחה את ה-martingale ב-incident: היא
                # נספרה כהפסד מלא והסלימה את המכפיל. כעת — ביטול + החזר סטייק, ו-
                # realized_pnl=None כדי שלא יזין שחזור-הפסד; נרשמת תקלה למעקב.
                trade["type"] = "SETTLE_UNKNOWN"
                trade["price"] = 0.0
                trade["fee_est"] = 0.0
                trade["realized_pnl"] = None
                trade["voided"] = True
                trade["settlement_error"] = "btc_prices_unavailable"
                trade["settlement_condition"] = "מחיר BTC לא זמין — בוטל, הסטייק הוחזר"
                self.state.balance_usd += leg_cost
                try:
                    from fault_tracker import record_fault
                    record_fault(
                        category="settlement", severity="high",
                        title="מחיר BTC לא זמין בהתחשבנות — חלון בוטל",
                        detail=(f"epoch={ep} ws={ws} start={start_p} end={end_p} "
                                f"side={p.side} ×{p.contracts} — בוטל, הוחזרו ${leg_cost:.2f}"),
                        source="demo_engine.expire_all_outside_tokens",
                        context={"epoch": ep, "btc_start": start_p, "btc_end": end_p,
                                 "side": p.side, "refund": round(leg_cost, 2)},
                        dedup_key="settle_unknown:btc_prices_unavailable",
                    )
                except Exception:
                    pass
                self.state.trades.append(trade)
                self._audit_finalize_settle_trade(trade)
                created.append(trade)
                continue

            resolved_up = float(end_p) >= float(start_p)
            trade["resolved_outcome"] = "Up" if resolved_up else "Down"
            trade["resolved_up"] = resolved_up
            won = (p.side == "Up" and resolved_up) or (p.side == "Down" and not resolved_up)
            trade["settlement_won"] = won

            if won:
                proceeds = p.contracts * 1.0 * (1 - FEE_RATE)
                realized = proceeds - leg_cost
                self.state.balance_usd += proceeds
                trade["type"] = "SETTLE_WIN"
                trade["price"] = 1.0
                trade["fee_est"] = FEE_RATE * 1.0 * p.contracts
                trade["realized_pnl"] = realized
            else:
                trade["type"] = "SETTLE_LOSS"
                trade["price"] = 0.0
                trade["fee_est"] = 0.0
                trade["realized_pnl"] = -leg_cost
            self.state.trades.append(trade)
            self._audit_finalize_settle_trade(trade)
            created.append(trade)

        self.state.positions = keep
        eq = self._equity_marked_consistent()
        self.state.equity_history.append((time.time(), eq))
        self.save()
        return created

    def reconcile_live_state(
        self,
        real_balance_usd: Optional[float],
        real_positions: list[dict[str, Any]],
        *,
        context: Optional[dict[str, Any]] = None,
        exclude_token_ids: Optional[set[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """מסנכרן את הספר הפנימי (ששימש כ-shadow ledger במצב לייב) ליתרה
        ולפוזיציות האמיתיות של Polymarket. נקרא אחרי epoch rollover ובקצב קבוע.

        מחזיר רשומת trade מסוג RECONCILE אם היה דלתא בכיס ($0.01+), אחרת None.
        תמיד מחליף את state.positions לרשימת הפוזיציות האמיתיות (מטא־דאטה של
        _position_tracking נשמר לטוקנים שממשיכים להתקיים, נמחק לטוקנים שנעלמו).
        """
        ctx = dict(context or {})
        reconcile_trade: Optional[dict[str, Any]] = None

        if isinstance(real_balance_usd, (int, float)) and math.isfinite(float(real_balance_usd)):
            real_bal = float(real_balance_usd)
            shadow_bal = float(self.state.balance_usd)
            delta = real_bal - shadow_bal
            if abs(delta) >= 0.01:
                tr: dict[str, Any] = {
                    "id": str(uuid.uuid4())[:8],
                    "ts": time.time(),
                    "type": "RECONCILE",
                    "shadow_balance_usd": shadow_bal,
                    "real_balance_usd": real_bal,
                    "realized_pnl": delta,
                    "price": 0.0,
                    "fee_est": 0.0,
                    "contracts": 0,
                    "execution": "live",
                }
                tr.update(ctx)
                self.state.trades.append(tr)
                reconcile_trade = tr
            self.state.balance_usd = real_bal

        # בנה רשימת פוזיציות חדשה מהמציאות; שמור avg_cost מהצל אם יש, אחרת avg_price אמיתי.
        shadow_by_token = {p.token_id: p for p in self.state.positions}
        new_positions: list[Position] = []
        real_token_ids: set[str] = set()
        _exclude = exclude_token_ids or set()
        for rp in real_positions or []:
            tid = str(rp.get("token_id") or "").strip()
            if not tid:
                continue
            if tid in _exclude:
                continue
            size = rp.get("size")
            try:
                size_f = float(size)
            except (TypeError, ValueError):
                continue
            if size_f <= 0:
                continue
            real_token_ids.add(tid)
            side_raw = str(rp.get("side") or "Up")
            side: Side = "Down" if side_raw == "Down" else "Up"
            # עדיף avg_cost שכבר היה לנו (שלנו כולל עמלה), אחרת ניקח avg_price מה-API
            if tid in shadow_by_token:
                sp = shadow_by_token[tid]
                avg_cost = float(sp.avg_cost)
                window_epoch = sp.window_epoch
                window_sec = sp.window_sec
            else:
                avg_cost = float(rp.get("avg_price") or rp.get("mark_price") or 0.0)
                window_epoch = None
                window_sec = None
            new_positions.append(
                Position(
                    side=side,
                    contracts=size_f,
                    avg_cost=avg_cost,
                    token_id=tid,
                    window_epoch=window_epoch,
                    window_sec=window_sec,
                )
            )

        # נקה _position_tracking / _session_by_token לטוקנים שנעלמו מהמציאות
        removed_tokens = [tid for tid in list(shadow_by_token.keys()) if tid not in real_token_ids]
        for tid in removed_tokens:
            self._position_tracking.pop(tid, None)
            self._session_by_token.pop(tid, None)

        self.state.positions = new_positions
        eq = self._equity_marked_consistent()
        self.state.equity_history.append((time.time(), eq))
        self.save()
        return reconcile_trade

    def export_csv(self, *, live_only: bool = False) -> str:
        """מייצר CSV: עסקאות + שדות מרכזיים. live_only=True — רק עסקאות מסחר חי (execution=live)."""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(
            [
                "ts",
                "time",
                "type",
                "side",
                "contracts",
                "price",
                "fee_est",
                "token_id",
                "session_id",
                "realized_pnl",
                "peak_unrealized_pct",
                "peak_ts",
                "peak_mark_bid",
                "trough_unrealized_pct",
                "trough_ts",
                "trough_mark_bid",
                "path_points",
                # context (אופציונלי)
                "epoch",
                "slug",
                "gate",
                "min_left_sec",
                "reason",
                "ask_u",
                "bid_u",
                "ask_d",
                "bid_d",
                "entry_target_usd",
                "limit_price",
            ]
        )
        rows = list(self.state.trades)
        if live_only:
            rows = [t for t in rows if str(t.get("execution") or "") == "live"]
        for t in rows:
            ts = float(t.get("ts") or 0)
            path = t.get("pnl_path") or []
            w.writerow(
                [
                    f"{ts:.3f}",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "",
                    t.get("type", ""),
                    t.get("side", ""),
                    t.get("contracts", ""),
                    t.get("price", ""),
                    t.get("fee_est", ""),
                    t.get("token_id", ""),
                    t.get("session_id", ""),
                    t.get("realized_pnl", ""),
                    t.get("peak_unrealized_pct", ""),
                    t.get("peak_ts", ""),
                    t.get("peak_mark_bid", ""),
                    t.get("trough_unrealized_pct", ""),
                    t.get("trough_ts", ""),
                    t.get("trough_mark_bid", ""),
                    len(path) if path else "",
                    t.get("epoch", ""),
                    t.get("slug", ""),
                    t.get("gate", ""),
                    t.get("min_left_sec", ""),
                    t.get("reason", ""),
                    t.get("ask_u", ""),
                    t.get("bid_u", ""),
                    t.get("ask_d", ""),
                    t.get("bid_d", ""),
                    t.get("entry_target_usd", ""),
                    t.get("limit_price", ""),
                ]
            )
        # שורה ריקה + snapshot
        w.writerow([])
        lm = self.state.last_mark or {}
        w.writerow(["snapshot_ts", "equity", "unrealized_usd"])
        w.writerow([lm.get("ts", ""), lm.get("equity", ""), lm.get("unrealized_usd", "")])
        return out.getvalue()

    def _position_idx(self, token_id: str) -> int:
        for i, p in enumerate(self.state.positions):
            if p.token_id == token_id:
                return i
        return -1

    def _apply_window_meta_from_context(self, token_id: str, ctx: Optional[dict[str, Any]]) -> None:
        """שומר epoch/window_sec של שוק ה-BTC Up/Down על הפוזיציה (לפירוק בסוף חלון)."""
        if not ctx:
            return
        we = ctx.get("epoch")
        ws = ctx.get("window_sec")
        if we is None and ws is None:
            return
        idx = self._position_idx(token_id)
        if idx < 0:
            return
        p = self.state.positions[idx]
        if we is not None:
            p.window_epoch = int(we)
        if ws is not None:
            p.window_sec = int(ws)

    async def best_ask(self, token_id: str) -> Optional[float]:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
                timeout=15.0,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            asks = list(data.get("asks") or [])
            if not asks:
                return None
            try:
                asks.sort(key=lambda x: float(x["price"]))
            except Exception:
                pass
            return float(asks[0]["price"])

    async def simulate_market_buy(
        self,
        side: Side,
        token_id: str,
        contracts: float,
        limit_price: Optional[float] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        ctx = context or {}
        oms = float(ctx.get("order_min_size") or 5)
        ok_sz, n_adj, verr = validate_contracts_for_market(
            float(contracts), oms, bump_if_needed=True
        )
        if not ok_sz:
            return {"ok": False, "error": verr or "גודל לא תקין"}
        contracts = n_adj
        ask = await self.best_ask(token_id)
        if ask is None:
            return {"ok": False, "error": "אין ספר פקודות או אין Ask"}
        af = float(ask)
        if af < MIN_LEGIT_SHARE_PRICE_USD or af > MAX_LEGIT_SHARE_PRICE_USD:
            return {
                "ok": False,
                "error": f"Ask לא תקין ({ask}) — מחוץ לטווח {MIN_LEGIT_SHARE_PRICE_USD}–{MAX_LEGIT_SHARE_PRICE_USD}",
            }
        fill = ask if limit_price is None else min(ask, limit_price)
        ff = float(fill)
        if ff < MIN_LEGIT_SHARE_PRICE_USD or ff > MAX_LEGIT_SHARE_PRICE_USD:
            return {
                "ok": False,
                "error": f"מחיר מילוי לא תקין ({fill}) — מחוץ לטווח {MIN_LEGIT_SHARE_PRICE_USD}–{MAX_LEGIT_SHARE_PRICE_USD}",
            }
        if limit_price is not None and ask > limit_price:
            return {"ok": False, "error": f"ה-Ask ({ask:.2f}) מעל הלימיט ({limit_price:.2f})"}
        cost = fill * contracts * (1 + FEE_RATE)
        if cost > self.state.balance_usd + 1e-9:
            return {"ok": False, "error": f"אין יתרה מספקת (נדרש ~{cost:.2f}$)"}

        self.state.balance_usd -= cost
        idx = self._position_idx(token_id)
        if idx >= 0:
            p = self.state.positions[idx]
            nc = p.contracts + contracts
            p.avg_cost = (p.avg_cost * p.contracts + fill * contracts) / nc if nc else fill
            p.contracts = nc
        else:
            self.state.positions.append(
                Position(side=side, contracts=contracts, avg_cost=fill, token_id=token_id)
            )
            self._position_tracking[token_id] = {
                "open_ts": time.time(),
                "high_watermark_pct": None,
                "high_watermark_ts": None,
                "high_mark_bid": None,
                "low_watermark_pct": None,
                "low_watermark_ts": None,
                "low_mark_bid": None,
                "path": [],
                "_last_path_ts": 0.0,
                "_last_path_upnl": None,
                "_prev_tick_upnl": None,
                "_volatile_until": 0.0,
            }
        self._apply_window_meta_from_context(token_id, context or {})
        tid = str(uuid.uuid4())[:8]
        sid = self._session_by_token.get(token_id)
        is_new_session = sid is None
        if is_new_session:
            sid = tid
            self._session_by_token[token_id] = sid
        trade = {
            "id": tid,
            "ts": time.time(),
            "side": side,
            "contracts": contracts,
            "price": fill,
            "fee_est": FEE_RATE * fill * contracts,
            "type": "BUY",
            "token_id": token_id,
            "session_id": sid,
        }
        # מספר מחזור ייחודי — נוצר רק עם פתיחת session חדש (כניסה ראשונה, לא DCA נוסף)
        if is_new_session:
            trade["trade_num"] = self.state.next_trade_num()
        if context:
            # audit_inputs is consumed out-of-band by the audit hook below; it must NOT
            # ride onto the persisted trade (keeps demo_state.json lean + JSON-safe).
            trade.update({_k: _v for _k, _v in context.items() if _k != "audit_inputs"})
        self.state.trades.append(trade)
        eq = self._equity_marked_consistent()
        self.state.equity_history.append((time.time(), eq))
        self.save()
        # ── Audit ledger: open the decision-time row (best-effort, never blocks). ──
        try:
            import audit_tracker, audit_snapshot, time as _t
            _inp = (context or {}).get("audit_inputs")
            if _inp and trade.get("session_id"):
                _inp = dict(_inp)
                _btc_spot = _inp.get("btc_spot_at_entry")  # stamped by strategy_runner from the cached TA
                for _k in ("side", "decision_ts_ms", "btc_spot_at_entry", "execution"):
                    _inp.pop(_k, None)
                _snap = audit_snapshot.build_decision_snapshot(
                    side=trade.get("side"),
                    decision_ts_ms=int(_t.time() * 1000), btc_spot_at_entry=_btc_spot,
                    execution={"avg_fill_price": trade.get("price"),
                               "contracts": trade.get("contracts"),
                               "gate": (context or {}).get("gate"),
                               "reason": (context or {}).get("reason"),
                               "investment_usd_effective": (context or {}).get("effective_investment_usd")},
                    **_inp)
                audit_tracker.open_row(str(trade["session_id"]), _snap)
        except Exception as _e:
            print(f"[audit] open_row hook failed (non-fatal): {_e!r}", flush=True)
        return {"ok": True, "trade": trade, "balance": self.state.balance_usd}

    def record_live_buy(
        self,
        side: Side,
        token_id: str,
        contracts: float,
        fill_price: float,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """שיקוף קנייה לייב — בלי קריאה לספר; מחיר מילוי ידוע מה-CLOB."""
        fill = float(fill_price)
        c = float(contracts)
        cost = fill * c * (1 + FEE_RATE)
        if cost > self.state.balance_usd + 1e-9:
            return {"ok": False, "error": f"יתרת דמו לא מספקת לשיקוף (~{cost:.2f}$)"}
        self.state.balance_usd -= cost
        idx = self._position_idx(token_id)
        if idx >= 0:
            p = self.state.positions[idx]
            nc = p.contracts + c
            p.avg_cost = (p.avg_cost * p.contracts + fill * c) / nc if nc else fill
            p.contracts = nc
        else:
            self.state.positions.append(
                Position(side=side, contracts=c, avg_cost=fill, token_id=token_id)
            )
            self._position_tracking[token_id] = {
                "open_ts": time.time(),
                "high_watermark_pct": None,
                "high_watermark_ts": None,
                "high_mark_bid": None,
                "low_watermark_pct": None,
                "low_watermark_ts": None,
                "low_mark_bid": None,
                "path": [],
                "_last_path_ts": 0.0,
                "_last_path_upnl": None,
                "_prev_tick_upnl": None,
                "_volatile_until": 0.0,
            }
        self._apply_window_meta_from_context(token_id, context or {})
        tid = str(uuid.uuid4())[:8]
        sid = self._session_by_token.get(token_id)
        if sid is None:
            sid = tid
            self._session_by_token[token_id] = sid
        trade = {
            "id": tid,
            "ts": time.time(),
            "side": side,
            "contracts": c,
            "price": fill,
            "fee_est": FEE_RATE * fill * c,
            "type": "BUY",
            "token_id": token_id,
            "session_id": sid,
            "execution": "live",
        }
        if context:
            # audit_inputs is consumed out-of-band by the audit hook below; it must NOT
            # ride onto the persisted trade (keeps demo_state.json lean + JSON-safe).
            trade.update({_k: _v for _k, _v in context.items() if _k != "audit_inputs"})
        self.state.trades.append(trade)
        eq = self._equity_marked_consistent()
        self.state.equity_history.append((time.time(), eq))
        self.save()
        # ── Audit ledger: open the decision-time row (best-effort, never blocks). ──
        try:
            import audit_tracker, audit_snapshot, time as _t
            _inp = (context or {}).get("audit_inputs")
            if _inp and trade.get("session_id"):
                _inp = dict(_inp)
                _btc_spot = _inp.get("btc_spot_at_entry")  # stamped by strategy_runner from the cached TA
                for _k in ("side", "decision_ts_ms", "btc_spot_at_entry", "execution"):
                    _inp.pop(_k, None)
                _snap = audit_snapshot.build_decision_snapshot(
                    side=trade.get("side"),
                    decision_ts_ms=int(_t.time() * 1000), btc_spot_at_entry=_btc_spot,
                    execution={"avg_fill_price": trade.get("price"),
                               "contracts": trade.get("contracts"),
                               "gate": (context or {}).get("gate"),
                               "reason": (context or {}).get("reason"),
                               "investment_usd_effective": (context or {}).get("effective_investment_usd")},
                    **_inp)
                audit_tracker.open_row(str(trade["session_id"]), _snap)
        except Exception as _e:
            print(f"[audit] open_row hook failed (non-fatal): {_e!r}", flush=True)
        return {"ok": True, "trade": trade, "balance": self.state.balance_usd}

    def _canonical_window_epoch_ws_for_session(self, session_id: str) -> tuple[Optional[int], int]:
        """חלון Polymarket לפי כניסה ראשונה (מסודרת לפי ts) — כולם באותו חלון 5m/15m חייבים אותו epoch."""
        buys = [
            t
            for t in self.state.trades
            if t.get("session_id") == session_id and t.get("type") == "BUY"
        ]
        if not buys:
            return None, 300
        buys.sort(key=lambda x: float(x.get("ts") or 0))
        for bt in buys:
            be = bt.get("epoch")
            if be is None:
                continue
            bws = int(bt.get("window_sec") or 300)
            return int(be), bws
        return None, int(buys[0].get("window_sec") or 300)

    def _infer_epoch_window_for_exit_trade(self, trade: dict[str, Any]) -> tuple[Optional[int], int]:
        """epoch/window_sec לפירוק BTC — קודם חלון הכניסה (BUY ראשון), לא epoch על TP (שהוא מ־tp_ctx ויכול להשתנות)."""
        sid = trade.get("session_id")
        if sid:
            ep, ws = self._canonical_window_epoch_ws_for_session(sid)
            if ep is not None:
                return ep, ws
        ep = trade.get("epoch")
        ws = int(trade.get("window_sec") or 300)
        if ep is not None:
            return int(ep), ws
        return None, ws

    async def _attach_window_btc_to_tp_trade(self, trade: dict[str, Any], *, side: str) -> None:
        """מחירי BTC בתחילת/סוף חלון (כמו בפירוק) — למעקב גם אחרי TP, לא רק SETTLE_*."""
        ep, ws = self._infer_epoch_window_for_exit_trade(trade)
        if ep is None:
            return
        trade["epoch"] = ep
        trade["window_sec"] = ws
        try:
            from btc_price import fetch_window_start_end_btc_usd

            px = await fetch_window_start_end_btc_usd(int(ep), int(ws))
        except Exception:
            return
        start_p = px.get("start")
        end_p = px.get("end")
        if start_p is not None:
            trade["settlement_btc_start"] = float(start_p)
        if end_p is not None:
            trade["settlement_btc_end"] = float(end_p)
        trade["settlement_price_source"] = px.get("source", "binance_1m_proxy")
        trade["settlement_condition"] = trade.get(
            "settlement_condition",
            "BTC בסוף החלון ≥ BTC בתחילת החלון ⇒ Up (פרוקסי Binance, לא Chainlink)",
        )
        if start_p is not None and end_p is not None:
            resolved_up = float(end_p) >= float(start_p)
            trade["resolved_outcome"] = "Up" if resolved_up else "Down"
            trade["settlement_won"] = (side == "Up" and resolved_up) or (side == "Down" and not resolved_up)

    def _build_session_canon_map(self) -> dict[str, tuple[Optional[int], int]]:
        """PR-D: בונה ב-pass יחיד מיפוי session_id -> (epoch, window_sec) של הכניסה הראשונה (BUY
        מוקדם ביותר עם epoch). מחליף קריאות _canonical_window_epoch_ws_for_session החוזרות (כל אחת
        O(N)) שגרמו ל-O(N²) ב-backfill על 50k עסקאות. סמנטיקה זהה ל-_canonical לצורך ה-backfill."""
        canon: dict[str, tuple[Optional[int], int]] = {}
        best_ts: dict[str, float] = {}
        for bt in self.state.trades:
            if bt.get("type") != "BUY":
                continue
            bsid = bt.get("session_id")
            if not bsid:
                continue
            be = bt.get("epoch")
            if be is None:
                continue
            bts = float(bt.get("ts") or 0)
            if bsid not in canon or bts < best_ts.get(bsid, 1e18):
                canon[bsid] = (int(be), int(bt.get("window_sec") or 300))
                best_ts[bsid] = bts
        return canon

    async def _backfill_missing_tp_settlement_btc(self) -> bool:
        """אחרי סוף החלון — ממלא settlement ל-SELL_TP שלא קיבלו נר סוף בזמן ה-TP (נר עדיין לא נסגר ב-Binance)."""
        now = time.time()
        changed = False
        # נקרא מ-mark_to_market — מגבילים קריאות Binance לתקציב לכל הופעה (מילוי הדרגתי).
        budget = 16
        # PR-D: precompute canonical-window map פעם אחת (O(N)) במקום קריאה O(N) לכל SELL_TP (O(N²)).
        session_canon = self._build_session_canon_map()
        for t in self.state.trades:
            if budget <= 0:
                break
            if t.get("type") != "SELL_TP":
                continue
            sid_tp = t.get("session_id")
            canon_ep, canon_ws = session_canon.get(str(sid_tp), (None, 300)) if sid_tp else (None, 300)
            # ep/ws לפירוק: קודם החלון הקנוני (כמו _infer_epoch_window_for_exit_trade), אחרת של ה-trade עצמו.
            if canon_ep is not None:
                ep, ws = canon_ep, canon_ws
            else:
                ep = t.get("epoch")
                ws = int(t.get("window_sec") or 300)
            if ep is None:
                continue
            if now < float(ep) + float(ws) - 0.25:
                continue
            tp_ep = t.get("epoch")
            epoch_mismatch = (
                bool(sid_tp)
                and canon_ep is not None
                and tp_ep is not None
                and int(tp_ep) != int(canon_ep)
            )
            if (
                t.get("settlement_btc_start") is not None
                and t.get("settlement_btc_end") is not None
                and not epoch_mismatch
            ):
                continue
            if epoch_mismatch:
                t.pop("settlement_btc_start", None)
                t.pop("settlement_btc_end", None)
                t.pop("resolved_outcome", None)
                t.pop("settlement_won", None)
            await self._attach_window_btc_to_tp_trade(t, side=str(t.get("side") or "Up"))
            budget -= 1
            if t.get("settlement_btc_start") is not None and t.get("settlement_btc_end") is not None:
                changed = True
        return changed

    async def record_live_sell(
        self,
        token_id: str,
        bid: float,
        context: Optional[dict[str, Any]] = None,
        *,
        contracts_sold: Optional[float] = None,
    ) -> dict[str, Any]:
        """שיקוף מכירה לייב — בלי קריאה לספר."""
        idx = self._position_idx(token_id)
        if idx < 0:
            return {"ok": False, "error": "אין פוזיציה"}
        p = self.state.positions[idx]
        bid = float(bid)
        full = float(p.contracts)
        sold = float(contracts_sold) if contracts_sold is not None else full
        sold = min(max(sold, 0.0), full)
        if sold < 1e-8:
            return {"ok": False, "error": "גודל מכירה אפס"}
        remainder = full - sold
        full_exit = remainder <= 1e-6

        proceeds = bid * sold * (1 - FEE_RATE)
        self.state.balance_usd += proceeds
        leg_cost = p.avg_cost * sold * (1 + FEE_RATE)
        realized = proceeds - leg_cost
        trade = {
            "id": str(uuid.uuid4())[:8],
            "ts": time.time(),
            "side": p.side,
            "contracts": sold,
            "price": bid,
            "fee_est": FEE_RATE * bid * sold,
            "type": ("SELL_STOP" if realized < 0 else "SELL_TP"),
            "token_id": token_id,
            "realized_pnl": realized,
            "leg_cost": leg_cost,
            "execution": "live",
        }
        if full_exit:
            sid = self._session_by_token.pop(token_id, None)
            if sid:
                trade["session_id"] = sid
            tr = self._position_tracking.pop(token_id, None)
            if tr:
                trade["peak_unrealized_pct"] = tr.get("high_watermark_pct")
                trade["peak_ts"] = tr.get("high_watermark_ts")
                trade["peak_mark_bid"] = tr.get("high_mark_bid")
                trade["trough_unrealized_pct"] = tr.get("low_watermark_pct")
                trade["trough_ts"] = tr.get("low_watermark_ts")
                trade["trough_mark_bid"] = tr.get("low_mark_bid")
                trade["pnl_path"] = _trim_settled_path(tr)
                trade["open_ts"] = tr.get("open_ts")
            self.state.positions.pop(idx)
        else:
            p.contracts = remainder
            sid = self._session_by_token.get(token_id)
            if sid:
                trade["session_id"] = sid
            tr = self._position_tracking.get(token_id)
            if tr:
                trade["peak_unrealized_pct"] = tr.get("high_watermark_pct")
                trade["peak_ts"] = tr.get("high_watermark_ts")
                trade["peak_mark_bid"] = tr.get("high_mark_bid")
                trade["trough_unrealized_pct"] = tr.get("low_watermark_pct")
                trade["trough_ts"] = tr.get("low_watermark_ts")
                trade["trough_mark_bid"] = tr.get("low_mark_bid")
                trade["pnl_path"] = _trim_settled_path(tr)
                trade["open_ts"] = tr.get("open_ts")
        if context:
            # audit_inputs is consumed out-of-band by the audit hook below; it must NOT
            # ride onto the persisted trade (keeps demo_state.json lean + JSON-safe).
            trade.update({_k: _v for _k, _v in context.items() if _k != "audit_inputs"})
        await self._attach_window_btc_to_tp_trade(trade, side=p.side)
        self.state.trades.append(trade)
        epoch = context.get("epoch") if context else None
        if full_exit and epoch is not None:
            ws = float(context.get("window_sec") or 300)
            window_end_ts = float(epoch) + ws
            leg_cost2 = p.avg_cost * sold * (1 + FEE_RATE)
            self._post_exit_tracking[token_id] = {
                "avg_cost": p.avg_cost,
                "contracts": sold,
                "leg_cost": leg_cost2,
                "window_end_ts": window_end_ts,
                "potential_high_pct": None,
                "potential_low_pct": None,
            }
        eq = self._equity_marked_consistent()
        self.state.equity_history.append((time.time(), eq))
        self.save()
        # ── Audit ledger: finalize the row with the outcome (best-effort). Only on FULL exit. ──
        if full_exit:
            try:
                import audit_tracker, time as _t
                if trade.get("session_id"):
                    audit_tracker.finalize_row(str(trade["session_id"]), {
                        "type": trade.get("type"),
                        "exit_type": ("TP" if trade.get("type") == "SELL_TP"
                                      else "stop" if trade.get("type") == "SELL_STOP"
                                      else "voided" if trade.get("voided")
                                      else "settle"),
                        "realized_pnl": trade.get("realized_pnl"),
                        "realized_pct": (round(100.0 * trade["realized_pnl"] /
                                               max(1e-9, trade.get("leg_cost") or 0), 4)
                                         if trade.get("realized_pnl") is not None and trade.get("leg_cost") else None),
                        "peak_unrealized_pct": trade.get("peak_unrealized_pct"),
                        "trough_unrealized_pct": trade.get("trough_unrealized_pct"),
                        "hold_duration_sec": (trade.get("ts", 0) - (trade.get("open_ts") or trade.get("ts", 0))),
                        "fees": trade.get("fee_est"),
                        "settlement_btc_start": trade.get("settlement_btc_start"),
                        "settlement_btc_end": trade.get("settlement_btc_end"),
                        "resolved_outcome": trade.get("resolved_outcome"),
                        "settlement_pnl_if_held": _settlement_pnl_if_held(trade),
                        "voided": trade.get("voided"),
                        "settlement_error": trade.get("settlement_error"),
                        "settled_ts": int(float(trade.get("ts") or _t.time()) * 1000),
                        "pnl_path": trade.get("pnl_path") or [],
                        "fee_rate": 0.0,
                    })
            except Exception as _e:
                print(f"[audit] finalize_row hook failed (non-fatal): {_e!r}", flush=True)
        return {
            "ok": True,
            "trade": trade,
            "balance": self.state.balance_usd,
            "full_exit": full_exit,
        }

    async def simulate_sell_all(
        self,
        token_id: str,
        bid_price: Optional[float] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        idx = self._position_idx(token_id)
        if idx < 0:
            return {"ok": False, "error": "אין פוזיציה"}
        p = self.state.positions[idx]
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
                timeout=15.0,
            )
            if r.status_code != 200:
                return {"ok": False, "error": "לא ניתן לקרוא ספר"}
            bids = list((r.json().get("bids") or []))
            if not bids:
                return {"ok": False, "error": "אין ביקוש"}
            try:
                bids.sort(key=lambda x: float(x["price"]), reverse=True)
            except Exception:
                pass
            bid = float(bids[0]["price"])
        if bid_price is not None:
            bid = min(bid, bid_price)
        proceeds = bid * p.contracts * (1 - FEE_RATE)
        self.state.balance_usd += proceeds
        # רווח/הפסד ממומש לעסקה זו (כולל עמלה בקירוב)
        leg_cost = p.avg_cost * p.contracts * (1 + FEE_RATE)
        realized = proceeds - leg_cost
        sid = self._session_by_token.pop(token_id, None)
        trade = {
            "id": str(uuid.uuid4())[:8],
            "ts": time.time(),
            "side": p.side,
            "contracts": p.contracts,
            "price": bid,
            "fee_est": FEE_RATE * bid * p.contracts,
            "type": ("SELL_STOP" if realized < 0 else "SELL_TP"),
            "token_id": token_id,
            "realized_pnl": realized,
            "leg_cost": leg_cost,
        }
        if sid:
            trade["session_id"] = sid
        tr = self._position_tracking.pop(token_id, None)
        if tr:
            trade["peak_unrealized_pct"] = tr.get("high_watermark_pct")
            trade["peak_ts"] = tr.get("high_watermark_ts")
            trade["peak_mark_bid"] = tr.get("high_mark_bid")
            trade["trough_unrealized_pct"] = tr.get("low_watermark_pct")
            trade["trough_ts"] = tr.get("low_watermark_ts")
            trade["trough_mark_bid"] = tr.get("low_mark_bid")
            trade["pnl_path"] = _trim_settled_path(tr)
            trade["open_ts"] = tr.get("open_ts")
        if context:
            # audit_inputs is consumed out-of-band by the audit hook below; it must NOT
            # ride onto the persisted trade (keeps demo_state.json lean + JSON-safe).
            trade.update({_k: _v for _k, _v in context.items() if _k != "audit_inputs"})
        await self._attach_window_btc_to_tp_trade(trade, side=p.side)
        self.state.trades.append(trade)
        self.state.positions.pop(idx)
        # מעקב שיא/שפל פוטנציאלי — מה שקרה אחרי היציאה עד סיום החלון
        epoch = context.get("epoch") if context else None
        if epoch is not None:
            ws = float(context.get("window_sec") or 300)
            window_end_ts = float(epoch) + ws
            leg_cost = p.avg_cost * p.contracts * (1 + FEE_RATE)
            self._post_exit_tracking[token_id] = {
                "avg_cost": p.avg_cost,
                "contracts": p.contracts,
                "leg_cost": leg_cost,
                "window_end_ts": window_end_ts,
                "potential_high_pct": None,
                "potential_low_pct": None,
            }
        eq = self._equity_marked_consistent()
        self.state.equity_history.append((time.time(), eq))
        self.save()
        # ── Audit ledger: finalize the row with the outcome (best-effort). Full exit (sells all). ──
        try:
            import audit_tracker, time as _t
            if trade.get("session_id"):
                audit_tracker.finalize_row(str(trade["session_id"]), {
                    "type": trade.get("type"),
                    "exit_type": ("TP" if trade.get("type") == "SELL_TP"
                                  else "stop" if trade.get("type") == "SELL_STOP"
                                  else "voided" if trade.get("voided")
                                  else "settle"),
                    "realized_pnl": trade.get("realized_pnl"),
                    "realized_pct": (round(100.0 * trade["realized_pnl"] /
                                           max(1e-9, trade.get("leg_cost") or 0), 4)
                                     if trade.get("realized_pnl") is not None and trade.get("leg_cost") else None),
                    "peak_unrealized_pct": trade.get("peak_unrealized_pct"),
                    "trough_unrealized_pct": trade.get("trough_unrealized_pct"),
                    "hold_duration_sec": (trade.get("ts", 0) - (trade.get("open_ts") or trade.get("ts", 0))),
                    "fees": trade.get("fee_est"),
                    "settlement_btc_start": trade.get("settlement_btc_start"),
                    "settlement_btc_end": trade.get("settlement_btc_end"),
                    "resolved_outcome": trade.get("resolved_outcome"),
                    "settlement_pnl_if_held": _settlement_pnl_if_held(trade),
                    "voided": trade.get("voided"),
                    "settlement_error": trade.get("settlement_error"),
                    "settled_ts": int(float(trade.get("ts") or _t.time()) * 1000),
                    "pnl_path": trade.get("pnl_path") or [],
                    "fee_rate": 0.0,
                })
        except Exception as _e:
            print(f"[audit] finalize_row hook failed (non-fatal): {_e!r}", flush=True)
        return {"ok": True, "trade": trade, "balance": self.state.balance_usd}

    def _audit_finalize_settle_trade(self, trade: dict[str, Any]) -> None:
        # ── Audit ledger: finalize a settled (SETTLE_*) row with the outcome (best-effort). ──
        try:
            import audit_tracker, time as _t
            if trade.get("session_id"):
                audit_tracker.finalize_row(str(trade["session_id"]), {
                    "type": trade.get("type"),
                    "exit_type": ("TP" if trade.get("type") == "SELL_TP"
                                  else "stop" if trade.get("type") == "SELL_STOP"
                                  else "voided" if trade.get("voided")
                                  else "settle"),
                    "realized_pnl": trade.get("realized_pnl"),
                    "realized_pct": (round(100.0 * trade["realized_pnl"] /
                                           max(1e-9, trade.get("leg_cost") or 0), 4)
                                     if trade.get("realized_pnl") is not None and trade.get("leg_cost") else None),
                    "peak_unrealized_pct": trade.get("peak_unrealized_pct"),
                    "trough_unrealized_pct": trade.get("trough_unrealized_pct"),
                    "hold_duration_sec": (trade.get("ts", 0) - (trade.get("open_ts") or trade.get("ts", 0))),
                    "fees": trade.get("fee_est"),
                    "settlement_btc_start": trade.get("settlement_btc_start"),
                    "settlement_btc_end": trade.get("settlement_btc_end"),
                    "resolved_outcome": trade.get("resolved_outcome"),
                    "settlement_pnl_if_held": _settlement_pnl_if_held(trade),
                    "voided": trade.get("voided"),
                    "settlement_error": trade.get("settlement_error"),
                    "settled_ts": int(float(trade.get("ts") or _t.time()) * 1000),
                    "pnl_path": trade.get("pnl_path") or [],
                    "fee_rate": 0.0,
                })
        except Exception as _e:
            print(f"[audit] finalize_row hook failed (non-fatal): {_e!r}", flush=True)

    def _mark_dirty(self) -> None:
        """PR-D: מסמן שיש שינוי שצריך persist — נכתב בפועל ע"י _maybe_persist_async (מושהה, מחוץ ללולאה)."""
        self._dirty = True

    async def _maybe_persist_async(self) -> None:
        """PR-D: persist מושהה (לכל היותר כל PERSIST_INTERVAL_SEC), single-flight, fire-and-forget.
        ה-snapshot נבנה על ה-event loop (to_dict), וה-json.dumps+כתיבה+fsync של 6MB רצים ב-thread
        כדי לא לחנוק את הלולאה (זה היה גורם ה-outage). הבקשה לא ממתינה לכתיבה."""
        now = time.time()
        if not self._dirty or self._persist_in_flight:
            return
        if (now - self._last_persist_ts) < PERSIST_INTERVAL_SEC:
            return
        self._persist_in_flight = True
        self._dirty = False
        snapshot = self.state.to_dict()  # נבנה על ה-loop — לעולם לא ב-thread (מוטציה במקביל)

        async def _run() -> None:
            try:
                await asyncio.to_thread(atomic_write_json, self.state_path, snapshot, indent=None)
                self._last_persist_ts = time.time()
            except Exception:
                self._dirty = True  # כשל — ננסה שוב בבקשה הבאה הזכאית
            finally:
                self._persist_in_flight = False

        asyncio.ensure_future(_run())  # fire-and-forget — הבקשה חוזרת מיד

    async def mark_to_market(self) -> dict[str, Any]:
        """מסמן את התיק לפי best bid (ערך מימוש משוער) כדי שהסטטיסטיקה תראה הפסד/רווח גם לפני יציאה."""
        # PR-D: ה-backfill הכבד (רשת + סריקה O(N²)) רץ לכל היותר כל BACKFILL_THROTTLE_SEC, לא בכל
        # poll. ה-save שלו הופך ל-persist מושהה מחוץ ללולאה (לא 6MB סינכרוני בכל poll).
        _now_bf = time.time()
        if (_now_bf - self._last_backfill_ts) >= BACKFILL_THROTTLE_SEC:
            self._last_backfill_ts = _now_bf
            if await self._backfill_missing_tp_settlement_btc():
                self._mark_dirty()
        # Throttle: WS cache makes reads instant, so we can be more aggressive.
        try:
            last_ts = float((self.state.last_mark or {}).get("ts") or 0.0)
            pos_ids = {p.token_id for p in self.state.positions}
            leg_rows = (self.state.last_mark or {}).get("legs") or []
            leg_tids = {x.get("token_id") for x in leg_rows if x.get("token_id")}
            missing_leg_for_position = bool(pos_ids) and not pos_ids.issubset(leg_tids)
            now_gate = time.time()
            aggressive_mark = now_gate < float(getattr(self, "_mark_aggressive_until", 0.0) or 0.0)
            ws_available = False
            try:
                from ws_price_stream import price_stream
                ws_available = price_stream.connected
            except Exception:
                pass
            if self.state.positions:
                if ws_available:
                    throttle_sec = 0.10 if aggressive_mark else 0.15
                else:
                    throttle_sec = MARK_THROTTLE_VOLATILE_SEC if aggressive_mark else 0.38
            else:
                throttle_sec = 0.25 if ws_available else 0.55
            if last_ts and (now_gate - last_ts) < throttle_sec and not missing_leg_for_position:
                # אין פוזיציות — מאפסים unrealized גם בתוך חלון ה-throttle
                if not self.state.positions and self.state.last_mark:
                    self.state.last_mark["unrealized_usd"] = 0.0
                    self.state.last_mark["legs"] = []
                    # חייבים ליישר equity ליתרה — אחרת נשאר equity ישן עם מימוש פתוח ונוצרת קפיצה ב־P&L / שידור
                    self.state.last_mark["equity"] = float(self.state.balance_usd)
                return self.state.last_mark
        except Exception:
            pass

        now = time.time()
        legs = []
        unreal = 0.0
        value = 0.0
        eq = self.state.balance_usd
        if not self.state.positions:
            self.state.last_mark = {"equity": eq, "unrealized_usd": 0.0, "ts": now, "legs": []}

        client = _get_demo_clob_httpx()
        if self.state.positions:
            for p in list(self.state.positions):
                bid: float | None = None
                try:
                    from ws_price_stream import price_stream

                    tp = price_stream.get_price(p.token_id)
                    if tp and tp.bid is not None and (now - tp.ts) < 30.0:
                        bid = tp.bid
                except Exception:
                    pass
                if bid is None:
                    r = await client.get(
                        "https://clob.polymarket.com/book",
                        params={"token_id": p.token_id},
                        timeout=4.0,  # F4: 8->4 — cap the WS-down fallback latency
                    )
                    if r.status_code != 200:
                        continue
                    bids = list((r.json().get("bids") or []))
                    if not bids:
                        continue
                    try:
                        bids.sort(key=lambda x: float(x["price"]), reverse=True)
                    except Exception:
                        pass
                    bid = float(bids[0]["price"])
                leg_value = bid * p.contracts * (1 - FEE_RATE)
                leg_cost = p.avg_cost * p.contracts * (1 + FEE_RATE)
                leg_unreal = leg_value - leg_cost
                leg_unreal_pct = (leg_unreal / leg_cost * 100.0) if leg_cost > 0 else 0.0
                value += leg_value
                unreal += leg_unreal
                legs.append(
                    {
                        "side": p.side,
                        "token_id": p.token_id,
                        "contracts": p.contracts,
                        "avg_cost": p.avg_cost,
                        "mark_bid": bid,
                        "leg_value": leg_value,
                        "leg_unrealized": leg_unreal,
                        "unrealized_pct": round(leg_unreal_pct, 2),
                    }
                )
            # אם CLOB החזיר שגיאה/בלי bids לכל הפוזיציות — לא לדרוס last_mark ב־legs ריק (הגרף נעלם)
            stale_fallback = False
            if not legs:
                prev_lm = self.state.last_mark or {}
                pl = prev_lm.get("legs") or []
                pos_ids = {p.token_id for p in self.state.positions}
                for x in pl:
                    tid = x.get("token_id")
                    if tid in pos_ids:
                        d = dict(x)
                        d["book_stale"] = True
                        legs.append(d)
                stale_fallback = bool(legs)

            if legs:
                if stale_fallback:
                    prev_lm = self.state.last_mark or {}
                    eq = float(prev_lm.get("equity") or self.state.balance_usd)
                    unreal = float(prev_lm.get("unrealized_usd") or 0.0)
                    self.state.last_mark = {
                        "equity": eq,
                        "unrealized_usd": unreal,
                        "ts": now,
                        "legs": legs,
                        "book_stale": True,
                    }
                else:
                    eq = self.state.balance_usd + value
                    self.state.last_mark = {"equity": eq, "unrealized_usd": unreal, "ts": now, "legs": legs}
                    self.state.equity_history.append((now, eq))

        # מעקב שיא/שפל פוטנציאלי — מה שקרה אחרי TP עד סיום החלון
        to_remove = []
        for tid, pe in list(self._post_exit_tracking.items()):
            if now >= pe["window_end_ts"]:
                # סיום החלון — מעדכנים את העסקה ומסירים מהמעקב
                done_trade: Optional[dict] = None
                for t in reversed(self.state.trades):
                    if t.get("token_id") == tid and t.get("type") == "SELL_TP":
                        if pe.get("potential_high_pct") is not None:
                            t["potential_peak_unrealized_pct"] = pe["potential_high_pct"]
                        if pe.get("potential_low_pct") is not None:
                            t["potential_trough_unrealized_pct"] = pe["potential_low_pct"]
                        done_trade = t
                        break
                if done_trade is not None:
                    if (
                        done_trade.get("settlement_btc_start") is None
                        or done_trade.get("settlement_btc_end") is None
                    ):
                        await self._attach_window_btc_to_tp_trade(
                            done_trade, side=str(done_trade.get("side") or "Up")
                        )
                    try:
                        from run_logging import log_potential_window_closed

                        log_potential_window_closed(done_trade)
                    except Exception:
                        pass
                to_remove.append(tid)
                continue
            # F4: WS-first + per-token 2s throttle + 4s timeout. peak/trough tracking is cosmetic,
            # so a cold WS must not make /api/demo/state pay M sequential 15s CLOB GETs every mark
            # (this was the residual ~4s after the save/backfill fix). When WS is up it's near-free.
            bid = None
            try:
                from ws_price_stream import price_stream
                _tp = price_stream.get_price(tid)
                if _tp and _tp.bid is not None and (now - _tp.ts) < 30.0:
                    bid = _tp.bid
            except Exception:
                pass
            if bid is None:
                if now - float(pe.get("_last_book_poll_ts", 0.0)) < 2.0:
                    continue  # throttle the REST fallback per token
                pe["_last_book_poll_ts"] = now
                r = await client.get(
                    "https://clob.polymarket.com/book",
                    params={"token_id": tid},
                    timeout=4.0,
                )
                if r.status_code != 200:
                    continue
                bids = list((r.json().get("bids") or []))
                if not bids:
                    continue
                try:
                    bids.sort(key=lambda x: float(x["price"]), reverse=True)
                except Exception:
                    pass
                bid = float(bids[0]["price"])
            leg_val = bid * pe["contracts"] * (1 - FEE_RATE)
            leg_cost = pe["leg_cost"]
            upnl_pct = (leg_val - leg_cost) / leg_cost * 100.0 if leg_cost > 0 else 0.0
            if pe["potential_high_pct"] is None or upnl_pct > pe["potential_high_pct"]:
                pe["potential_high_pct"] = upnl_pct
            if pe["potential_low_pct"] is None or upnl_pct < pe["potential_low_pct"]:
                pe["potential_low_pct"] = upnl_pct
            # עדכון העסקה בזמן אמת (להצגה ב-UI)
            for t in reversed(self.state.trades):
                if t.get("token_id") == tid and t.get("type") == "SELL_TP":
                    t["potential_peak_unrealized_pct"] = pe["potential_high_pct"]
                    t["potential_trough_unrealized_pct"] = pe["potential_low_pct"]
                    break
        for tid in to_remove:
            self._post_exit_tracking.pop(tid, None)

        # עדכון שיא/שפל ומסלול לכל פוזיציה (גם פוזיציות שנטענו מ-disk ללא tracking)
        for leg in legs:
            tid = leg["token_id"]
            tr = self._position_tracking.get(tid)
            if not tr:
                tr = {
                    "open_ts": now,
                    "high_watermark_pct": None,
                    "high_watermark_ts": None,
                    "high_mark_bid": None,
                    "low_watermark_pct": None,
                    "low_watermark_ts": None,
                    "low_mark_bid": None,
                    "path": [],
                    "_last_path_ts": 0.0,
                    "_last_path_upnl": None,
                    "_prev_tick_upnl": None,
                    "_volatile_until": 0.0,
                }
                self._position_tracking[tid] = tr
            leg_cost = leg["avg_cost"] * leg["contracts"] * (1 + FEE_RATE)
            upnl_pct = (leg["leg_unrealized"] / leg_cost * 100.0) if leg_cost > 0 else 0.0
            # קפיצה גדולה בין טיק mark_to_market מלא לקודם → חלון אגרסיבי (דגימת path צפופה + סימון מהיר)
            prev_tick = tr.get("_prev_tick_upnl")
            if prev_tick is not None and abs(upnl_pct - float(prev_tick)) >= VOLATILE_TICK_DELTA_PCT:
                tr["_volatile_until"] = now + VOLATILE_WINDOW_SEC
                self._mark_aggressive_until = now + VOLATILE_WINDOW_SEC
            # Watermarks
            if tr["high_watermark_pct"] is None or upnl_pct > tr["high_watermark_pct"]:
                tr["high_watermark_pct"] = upnl_pct
                tr["high_watermark_ts"] = now
                tr["high_mark_bid"] = leg["mark_bid"]
            if tr["low_watermark_pct"] is None or upnl_pct < tr["low_watermark_pct"]:
                tr["low_watermark_pct"] = upnl_pct
                tr["low_watermark_ts"] = now
                tr["low_mark_bid"] = leg["mark_bid"]
            leg["peak_unrealized_pct"] = tr.get("high_watermark_pct")
            leg["trough_unrealized_pct"] = tr.get("low_watermark_pct")
            leg["peak_ts"] = tr.get("high_watermark_ts")
            leg["trough_ts"] = tr.get("low_watermark_ts")
            leg["pnl_path"] = tr.get("path", [])
            # Path throttled — ts במילישניות כדי שלא יאבדו שתי דגימות שונות באותה שנייה ב־UI
            path = tr["path"]
            last_ts = tr.get("_last_path_ts") or 0.0
            last_upnl = tr.get("_last_path_upnl")
            jump = last_upnl is not None and abs(upnl_pct - last_upnl) >= POSITION_TRACKING_PATH_FORCE_DELTA_PCT
            if jump:
                tr["_volatile_until"] = max(float(tr.get("_volatile_until") or 0.0), now + VOLATILE_WINDOW_SEC)
                self._mark_aggressive_until = max(
                    float(getattr(self, "_mark_aggressive_until", 0.0) or 0.0),
                    now + VOLATILE_WINDOW_SEC,
                )
            in_volatile = float(tr.get("_volatile_until") or 0.0) > now
            path_iv = PATH_INTERVAL_VOLATILE_SEC if in_volatile else POSITION_TRACKING_PATH_INTERVAL
            do_sample = not leg.get("book_stale") and (
                last_upnl is None
                or (now - last_ts) >= path_iv
                or abs(upnl_pct - last_upnl) >= POSITION_TRACKING_PATH_MIN_DELTA_PCT
                or jump
            )
            if do_sample:
                path.append({
                    "ts": round(now, 3),
                    "upnl_pct": round(upnl_pct, 2),
                    "bid": round(leg["mark_bid"], 4),
                    "balance": round(self.state.balance_usd, 2),
                    "equity": round(eq, 2),
                })
                if len(path) > POSITION_TRACKING_PATH_MAX:
                    # Smart trim: שומר peak/trough/entry/recent במקום למחוק עיוור.
                    new_path = _smart_trim_path(
                        path,
                        POSITION_TRACKING_PATH_MAX,
                        tr.get("high_watermark_ts"),
                        tr.get("low_watermark_ts"),
                    )
                    path.clear()
                    path.extend(new_path)
                tr["_last_path_ts"] = now
                tr["_last_path_upnl"] = upnl_pct
            tr["_prev_tick_upnl"] = upnl_pct
        # PR-D: לא שומרים 6MB סינכרונית בכל mark (זה חנק את ה-event loop). מסמנים dirty ומבקשים
        # persist מושהה מחוץ ללולאה (fire-and-forget, לכל היותר כל 20s). המצב נשמר גם ב-BUY/SELL/
        # settlement (saves סינכרוניים שנשארו), אז אין סיכון לאיבוד נתונים קריטיים.
        self._mark_dirty()
        await self._maybe_persist_async()
        return self.state.last_mark

    def unrealized_pnl_pct(self, token_id: str, mark_bid: float) -> Optional[float]:
        idx = self._position_idx(token_id)
        if idx < 0:
            return None
        p = self.state.positions[idx]
        if p.avg_cost <= 0:
            return None
        return (mark_bid - p.avg_cost) / p.avg_cost * 100.0
