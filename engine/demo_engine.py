"""
מנוע דמו: יתרה וירטואלית, פוזיציות, מילוי מול ספר פקודות אמיתי.
עמלה: ~0.2% לצד (maker/taker 1000 ב-basis points של Polymarket — נלקח 0.2% כברירת מחדל).
"""
from __future__ import annotations

import json
import os
import time
import uuid
import csv
import io
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from order_validation import validate_contracts_for_market
from pricing_limits import MAX_LEGIT_SHARE_PRICE_USD, MIN_LEGIT_SHARE_PRICE_USD

FEE_RATE = 0.002  # 0.2% לצד כהערכה

Side = Literal["Up", "Down"]


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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DemoState":
        st = cls(
            balance_usd=float(d.get("balance_usd", 10_000)),
            trades=list(d.get("trades") or []),
            equity_history=[tuple(x) for x in (d.get("equity_history") or [])],
            trade_seq=int(d.get("trade_seq", 0)),
            loss_recovery_streak=int(d.get("loss_recovery_streak", 0) or 0),
            loss_recovery_multiplier=float(d.get("loss_recovery_multiplier", 1.0) or 1.0),
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
# גלילה: נשמרות ה־N האחרונות (לא מפסיקים לדגום אחרי N)
POSITION_TRACKING_PATH_MAX = 240

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
        self._backfill_session_ids()

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

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state.to_dict(), indent=2))

    def reset(self, balance: float = 10_000.0) -> None:
        self.state = DemoState(balance_usd=balance)
        self._position_tracking.clear()
        self._post_exit_tracking.clear()
        self._session_by_token.clear()
        self.save()

    def clear_stats(self) -> None:
        """איפוס סטטיסטיקה: מוחק היסטוריה ועסקאות, בלי לשנות יתרה/פוזיציות."""
        self._post_exit_tracking.clear()
        self.state.trades = []
        self.state.equity_history = []
        self.state.last_mark = {}
        self.save()

    async def reset_stats_and_flatten_positions(self) -> None:
        """איפוס סטטיסטיקה + סגירת פוזיציות פתוחות בלי ליצור עסקאות.
        
        המטרה: אחרי איפוס, לא תהיה תנועה של מדדים/גרף בגלל פוזיציות שנשארו פתוחות.
        """
        # 1) נקה היסטוריית סטטיסטיקה
        self.state.trades = []
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

        keep: list[Position] = []
        to_settle: list[Position] = []
        for p in list(self.state.positions):
            if p.token_id in valid_tokens:
                keep.append(p)
            else:
                to_settle.append(p)

        price_cache: dict[tuple[int, int], dict[str, Any]] = {}

        async def _prices_for(ep: int, ws: int) -> dict[str, Any]:
            key = (ep, ws)
            if key not in price_cache:
                price_cache[key] = await fetch_window_start_end_btc_usd(ep, ws)
            return price_cache[key]

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
            if sid:
                trade["session_id"] = sid
            if tr:
                trade["peak_unrealized_pct"] = tr.get("high_watermark_pct")
                trade["peak_ts"] = tr.get("high_watermark_ts")
                trade["peak_mark_bid"] = tr.get("high_mark_bid")
                trade["trough_unrealized_pct"] = tr.get("low_watermark_pct")
                trade["trough_ts"] = tr.get("low_watermark_ts")
                trade["trough_mark_bid"] = tr.get("low_mark_bid")
                trade["pnl_path"] = tr.get("path", [])

            trade.update(ctx)
            if ep is not None:
                trade["settled_epoch"] = ep

            if ep is None:
                trade["type"] = "SETTLE_UNKNOWN"
                trade["price"] = 0.0
                trade["fee_est"] = 0.0
                trade["realized_pnl"] = -leg_cost
                trade["settlement_error"] = "missing_window_epoch"
                trade["settlement_condition"] = "לא ניתן לחשב — חסר epoch לחלון"
                self.state.trades.append(trade)
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
                trade["type"] = "SETTLE_UNKNOWN"
                trade["price"] = 0.0
                trade["fee_est"] = 0.0
                trade["realized_pnl"] = -leg_cost
                trade["settlement_error"] = "btc_prices_unavailable"
                self.state.trades.append(trade)
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
            created.append(trade)

        self.state.positions = keep
        eq = self.state.balance_usd + sum(x.contracts * x.avg_cost for x in self.state.positions)
        self.state.equity_history.append((time.time(), eq))
        self.save()
        return created

    def export_csv(self) -> str:
        """מייצר CSV: עסקאות + שדות מרכזיים."""
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
        for t in self.state.trades:
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
            trade.update(context)
        self.state.trades.append(trade)
        eq = self.state.balance_usd + sum(
            p.contracts * p.avg_cost for p in self.state.positions
        )
        self.state.equity_history.append((time.time(), eq))
        self.save()
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
            trade.update(context)
        self.state.trades.append(trade)
        eq = self.state.balance_usd + sum(
            p.contracts * p.avg_cost for p in self.state.positions
        )
        self.state.equity_history.append((time.time(), eq))
        self.save()
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

    async def _backfill_missing_tp_settlement_btc(self) -> bool:
        """אחרי סוף החלון — ממלא settlement ל-SELL_TP שלא קיבלו נר סוף בזמן ה-TP (נר עדיין לא נסגר ב-Binance)."""
        now = time.time()
        changed = False
        # נקרא מ-mark_to_market בתדירות גבוהה — מגבילים קריאות Binance לעסקה אחת לכל הופעה (מספיק למילוי הדרגתי)
        budget = 16
        for t in self.state.trades:
            if budget <= 0:
                break
            if t.get("type") != "SELL_TP":
                continue
            ep, ws = self._infer_epoch_window_for_exit_trade(t)
            if ep is None:
                continue
            if now < float(ep) + float(ws) - 0.25:
                continue
            sid_tp = t.get("session_id")
            canon_ep, _ = (
                self._canonical_window_epoch_ws_for_session(str(sid_tp))
                if sid_tp
                else (None, 300)
            )
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
    ) -> dict[str, Any]:
        """שיקוף מכירה לייב — בלי קריאה לספר."""
        idx = self._position_idx(token_id)
        if idx < 0:
            return {"ok": False, "error": "אין פוזיציה"}
        p = self.state.positions[idx]
        bid = float(bid)
        proceeds = bid * p.contracts * (1 - FEE_RATE)
        self.state.balance_usd += proceeds
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
            "type": "SELL_TP",
            "token_id": token_id,
            "realized_pnl": realized,
            "execution": "live",
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
            trade["pnl_path"] = tr.get("path", [])
        if context:
            trade.update(context)
        await self._attach_window_btc_to_tp_trade(trade, side=p.side)
        self.state.trades.append(trade)
        self.state.positions.pop(idx)
        epoch = context.get("epoch") if context else None
        if epoch is not None:
            ws = float(context.get("window_sec") or 300)
            window_end_ts = float(epoch) + ws
            leg_cost2 = p.avg_cost * p.contracts * (1 + FEE_RATE)
            self._post_exit_tracking[token_id] = {
                "avg_cost": p.avg_cost,
                "contracts": p.contracts,
                "leg_cost": leg_cost2,
                "window_end_ts": window_end_ts,
                "potential_high_pct": None,
                "potential_low_pct": None,
            }
        eq = self.state.balance_usd + sum(
            x.contracts * x.avg_cost for x in self.state.positions
        )
        self.state.equity_history.append((time.time(), eq))
        self.save()
        return {"ok": True, "trade": trade, "balance": self.state.balance_usd}

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
            "type": "SELL_TP",
            "token_id": token_id,
            "realized_pnl": realized,
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
            trade["pnl_path"] = tr.get("path", [])
        if context:
            trade.update(context)
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
        eq = self.state.balance_usd + sum(
            x.contracts * x.avg_cost for x in self.state.positions
        )
        self.state.equity_history.append((time.time(), eq))
        self.save()
        return {"ok": True, "trade": trade, "balance": self.state.balance_usd}

    async def mark_to_market(self) -> dict[str, Any]:
        """מסמן את התיק לפי best bid (ערך מימוש משוער) כדי שהסטטיסטיקה תראה הפסד/רווח גם לפני יציאה."""
        # מילוי פירוק BTC ל-TP — בכל קריאה (מגבלה פנימית לעסקאות/קריאה) כדי שה-UI יקבל נתונים מיד אחרי /api/demo/state
        if await self._backfill_missing_tp_settlement_btc():
            self.save()
        # Throttle: לא לפגוע ב-CLOB; ~0.55s מאפשר לרענון UI כל 1s לקבל סימון מלא ודגימת מסלול עדכנית.
        # לא מדללים כשיש פוזיציה בלי רגל תואמת ב־last_mark (למשל כניסה מיד אחרי tick) — אחרת ה־UI
        # מקבל legs ריקים / בלי pnl_path ורואה גרף נעלם + placeholder "ממתין לדגימה…".
        try:
            last_ts = float((self.state.last_mark or {}).get("ts") or 0.0)
            pos_ids = {p.token_id for p in self.state.positions}
            leg_rows = (self.state.last_mark or {}).get("legs") or []
            leg_tids = {x.get("token_id") for x in leg_rows if x.get("token_id")}
            missing_leg_for_position = bool(pos_ids) and not pos_ids.issubset(leg_tids)
            now_gate = time.time()
            aggressive_mark = now_gate < float(getattr(self, "_mark_aggressive_until", 0.0) or 0.0)
            # פוזיציה פתוחה: רגיל ~0.38s; אחרי תנודתיות גבוהה בין טיקים — ~0.22s עד סוף החלון
            if self.state.positions:
                throttle_sec = MARK_THROTTLE_VOLATILE_SEC if aggressive_mark else 0.38
            else:
                throttle_sec = 0.55
            if last_ts and (now_gate - last_ts) < throttle_sec and not missing_leg_for_position:
                # אין פוזיציות — מאפסים unrealized גם בתוך חלון ה-throttle
                if not self.state.positions and self.state.last_mark:
                    self.state.last_mark["unrealized_usd"] = 0.0
                    self.state.last_mark["legs"] = []
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

        async with httpx.AsyncClient() as client:
            if self.state.positions:
                for p in list(self.state.positions):
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
                r = await client.get(
                    "https://clob.polymarket.com/book",
                    params={"token_id": tid},
                    timeout=15.0,
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
                while len(path) > POSITION_TRACKING_PATH_MAX:
                    path.pop(0)
                tr["_last_path_ts"] = now
                tr["_last_path_upnl"] = upnl_pct
            tr["_prev_tick_upnl"] = upnl_pct
        self.save()
        return self.state.last_mark

    def unrealized_pnl_pct(self, token_id: str, mark_bid: float) -> Optional[float]:
        idx = self._position_idx(token_id)
        if idx < 0:
            return None
        p = self.state.positions[idx]
        if p.avg_cost <= 0:
            return None
        return (mark_bid - p.avg_cost) / p.avg_cost * 100.0
