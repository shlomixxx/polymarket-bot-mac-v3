"""
WebSocket price stream — real-time bid/ask from Polymarket CLOB.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market,
subscribes to token IDs, and maintains an in-memory cache of best bid/ask.

Broadcasts price changes to all connected frontend WebSocket clients.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets
import websockets.exceptions

POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_SEC = 8
RECONNECT_DELAY_SEC = 1.0
RECONNECT_MAX_DELAY_SEC = 15.0
# אם לא הגיעה הודעה בזמן הזה — נסגור את ה-WS ונפתח מחדש (defensive against half-open).
STALE_RECONNECT_SEC = 30.0
# מתחת לסף הזה אנחנו מסמנים מחיר WS כ-"טרי" — מעל זה צרכן צריך לעשות fallback ל-HTTP.
FRESH_PRICE_MAX_AGE_SEC = 5.0
# כמה רמות עומק לשמור בכל צד (top-of-book). מספיק ל-CLOB imbalance, כמה KB לטוקן.
BOOK_DEPTH_LEVELS = 10


def _ssl_context_for_polymarket_ws() -> ssl.SSLContext:
    """ב-macOS/Python לעיתים חסר bundle של CA — certifi (תלות של httpx) פותר SSLCertVerificationError."""
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def _truncate_levels(levels: list[Any]) -> list[dict[str, float]]:
    """ממיר רמות ספר (כבר ממויינות) לרשימת {"price","size"} חתוכה ל-BOOK_DEPTH_LEVELS.

    מתאים בדיוק למה ש-clob_imbalance.compute_book_depth קורא (.get("size")/.get("price")
    ו-bids[0]["price"]). רמות פגומות מדולגות; אף פעם לא זורק.
    """
    out: list[dict[str, float]] = []
    for lvl in levels[:BOOK_DEPTH_LEVELS]:
        try:
            out.append({"price": float(lvl["price"]), "size": float(lvl["size"])})
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out


@dataclass
class TokenPrice:
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    ts: float = 0.0
    # עומק ספר הפקודות (עד BOOK_DEPTH_LEVELS רמות לכל צד) — מגיע "בחינם" על אותו WS
    # ומשרת את analyze_clob_imbalance. כל רמה היא {"price": float, "size": float}.
    bids: list[dict[str, float]] = field(default_factory=list)
    asks: list[dict[str, float]] = field(default_factory=list)
    # זמן עדכון העומק. מתעדכן יחד עם book/initial. נשמר בנפרד מ-ts (שמתעדכן גם
    # מ-price_change/best_bid_ask שלא נושאים עומק מלא).
    book_ts: float = 0.0

    def update_from_best_bid_ask(self, data: dict[str, Any]) -> bool:
        changed = False
        for change in data.get("changes", []):
            price = change.get("price")
            side = change.get("side")
            if price is not None and side:
                p = float(price)
                if side == "BUY" and (self.bid is None or abs(self.bid - p) > 1e-9):
                    self.bid = p
                    changed = True
                elif side == "SELL" and (self.ask is None or abs(self.ask - p) > 1e-9):
                    self.ask = p
                    changed = True
        if changed:
            self.ts = time.time()
            if self.bid is not None and self.ask is not None:
                self.mid = (self.bid + self.ask) / 2.0
            elif self.bid is not None:
                self.mid = self.bid
            elif self.ask is not None:
                self.mid = self.ask
        return changed

    def update_from_book(self, data: dict[str, Any]) -> bool:
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        old_bid, old_ask = self.bid, self.ask
        sorted_bids: list[Any] = []
        sorted_asks: list[Any] = []
        if bids:
            try:
                sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
                self.bid = float(sorted_bids[0]["price"])
            except (KeyError, IndexError, ValueError):
                sorted_bids = []
        if asks:
            try:
                sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
                self.ask = float(sorted_asks[0]["price"])
            except (KeyError, IndexError, ValueError):
                sorted_asks = []
        # שמירת עומק (top-N) בצורה ש-analyze_clob_imbalance מצפה לה: רשימת
        # {"price": float, "size": float}. ההפעלה הזו "בחינם" — העומק כבר על ה-WS.
        new_bids = _truncate_levels(sorted_bids)
        new_asks = _truncate_levels(sorted_asks)
        # מעדכנים depth רק אם ההודעה הביאה רמות (book/initial). price_change חלקי
        # לא יגיע לכאן עם bids/asks מלאים, אז לא נמחק עומק קיים לחינם.
        if new_bids or new_asks:
            self.bids = new_bids
            self.asks = new_asks
            self.book_ts = time.time()
        changed = self.bid != old_bid or self.ask != old_ask
        if changed:
            self.ts = time.time()
            if self.bid is not None and self.ask is not None:
                self.mid = (self.bid + self.ask) / 2.0
            elif self.bid is not None:
                self.mid = self.bid
            elif self.ask is not None:
                self.mid = self.ask
        return changed

    def update_from_price_change(self, data: dict[str, Any]) -> bool:
        side = data.get("side")
        price = data.get("price")
        if side is None or price is None:
            changes = data.get("changes") or []
            changed_any = False
            for c in changes:
                s = c.get("side")
                p = c.get("price")
                if s and p is not None:
                    pf = float(p)
                    if s == "BUY" and (self.bid is None or abs(self.bid - pf) > 1e-9):
                        self.bid = pf
                        changed_any = True
                    elif s == "SELL" and (self.ask is None or abs(self.ask - pf) > 1e-9):
                        self.ask = pf
                        changed_any = True
            if changed_any:
                self.ts = time.time()
                if self.bid is not None and self.ask is not None:
                    self.mid = (self.bid + self.ask) / 2.0
            return changed_any

        pf = float(price)
        old_bid, old_ask = self.bid, self.ask
        if side == "BUY":
            self.bid = pf
        elif side == "SELL":
            self.ask = pf
        changed = self.bid != old_bid or self.ask != old_ask
        if changed:
            self.ts = time.time()
            if self.bid is not None and self.ask is not None:
                self.mid = (self.bid + self.ask) / 2.0
        return changed


class FrontendStreamClient:
    """לקוח UI יחיד: שומר רק את ההודעה האחרונה לכל token_id.

    מטרה: לא לאבד עדכון מחיר של טוקן אחד בגלל שטוקן אחר זרק הודעות לתור.
    כל broadcast מחליף את המצב הקודם של אותו טוקן (ה-FE צריך תמיד את הטרי).
    """

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}
        self._event = asyncio.Event()
        self._closed = False

    def push(self, token_id: str, msg: str) -> None:
        if self._closed:
            return
        self._pending[token_id] = msg
        self._event.set()

    async def drain(self) -> list[str]:
        """ממתין להודעה אחת לפחות, ואז מחזיר את כל המעודכנים — אחד לטוקן."""
        await self._event.wait()
        msgs = list(self._pending.values())
        self._pending.clear()
        self._event.clear()
        return msgs

    async def drain_with_timeout(self, timeout: float) -> list[str]:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return []
        msgs = list(self._pending.values())
        self._pending.clear()
        self._event.clear()
        return msgs

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
        self._event.set()


class PriceStreamManager:
    """Manages WebSocket connection to Polymarket and broadcasts to frontend clients."""

    def __init__(self) -> None:
        self._prices: dict[str, TokenPrice] = {}
        self._subscribed_tokens: set[str] = set()
        self._ws: Any = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._running = False
        self._reconnect_delay = RECONNECT_DELAY_SEC
        self._frontend_clients: set[FrontendStreamClient] = set()
        self._on_price_change_callbacks: list[Callable[[str, TokenPrice], None]] = []
        self._connected = False
        self._last_msg_ts: float = 0.0
        self._token_to_side: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_ts(self) -> float:
        return self._last_msg_ts

    def is_stream_fresh(self, max_age_sec: float = FRESH_PRICE_MAX_AGE_SEC) -> bool:
        """האם ה-WS חי וקיבל הודעה לאחרונה. שימוש לוגיקת מסחר ל-fallback ל-HTTP."""
        if not self._connected:
            return False
        if self._last_msg_ts <= 0:
            return False
        return (time.time() - self._last_msg_ts) <= max_age_sec

    def get_price(self, token_id: str) -> Optional[TokenPrice]:
        return self._prices.get(token_id)

    def get_fresh_price(
        self, token_id: str, max_age_sec: float = FRESH_PRICE_MAX_AGE_SEC
    ) -> Optional[TokenPrice]:
        """מחזיר רק אם המחיר חדש מספיק; אחרת None — צרכן יפנה ל-HTTP fallback."""
        tp = self._prices.get(token_id)
        if tp is None or tp.ts <= 0:
            return None
        if (time.time() - tp.ts) > max_age_sec:
            return None
        return tp

    def get_book(
        self, token_id: str, *, max_age_sec: float = FRESH_PRICE_MAX_AGE_SEC
    ) -> Optional[dict[str, list[dict[str, float]]]]:
        """מחזיר את עומק הספר {"bids":[...], "asks":[...]} רק אם הוא טרי.

        משמש את analyze_clob_imbalance (data-only, ל-audit). אם אין עומק או שהוא
        stale (מעבר ל-max_age_sec) → None, כך ש-compute_signals יקבל None וה-clob
        sub-signal יישאר available=False (אף פעם לא נתון ישן/רע). אין fetch רשת — זה
        רק העומק שכבר הגיע על ה-WS.
        """
        tp = self._prices.get(token_id)
        if tp is None or tp.book_ts <= 0:
            return None
        if not tp.bids and not tp.asks:
            return None
        if (time.time() - tp.book_ts) > max_age_sec:
            return None
        # מחזירים עותקים רדודים כדי ש-caller לא ישנה את ה-cache הפנימי בטעות.
        return {"bids": list(tp.bids), "asks": list(tp.asks)}

    def get_best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        tp = self._prices.get(token_id)
        if tp is None:
            return None, None
        return tp.bid, tp.ask

    def register_frontend_client(self) -> FrontendStreamClient:
        client = FrontendStreamClient()
        self._frontend_clients.add(client)
        return client

    def unregister_frontend_client(self, client: FrontendStreamClient) -> None:
        try:
            client.close()
        except Exception:
            pass
        self._frontend_clients.discard(client)

    def _broadcast_to_frontend(self, token_id: str, tp: TokenPrice) -> None:
        side_label = self._token_to_side.get(token_id, "unknown")
        msg = json.dumps({
            "type": "price",
            "token_id": token_id,
            "side": side_label,
            "bid": tp.bid,
            "ask": tp.ask,
            "mid": tp.mid,
            "ts": tp.ts,
        })
        for client in list(self._frontend_clients):
            try:
                client.push(token_id, msg)
            except Exception:
                self._frontend_clients.discard(client)

    async def subscribe_tokens(
        self,
        token_up: str,
        token_down: str,
        *,
        token_side_map: Optional[dict[str, str]] = None,
    ) -> None:
        async with self._lock:
            new_tokens = {token_up, token_down}
            if token_side_map:
                self._token_to_side.update(token_side_map)
            else:
                self._token_to_side[token_up] = "Up"
                self._token_to_side[token_down] = "Down"

            for t in new_tokens:
                if t not in self._prices:
                    self._prices[t] = TokenPrice()

            tokens_to_add = new_tokens - self._subscribed_tokens
            tokens_to_remove = self._subscribed_tokens - new_tokens

            if tokens_to_remove and self._ws:
                try:
                    unsub_msg = json.dumps({
                        "assets_ids": list(tokens_to_remove),
                        "operation": "unsubscribe",
                    })
                    await self._ws.send(unsub_msg)
                except Exception:
                    pass
                for t in tokens_to_remove:
                    self._prices.pop(t, None)
                    self._token_to_side.pop(t, None)

            if tokens_to_add and self._ws:
                try:
                    sub_msg = json.dumps({
                        "assets_ids": list(tokens_to_add),
                        "operation": "subscribe",
                        "custom_feature_enabled": True,
                    })
                    await self._ws.send(sub_msg)
                except Exception:
                    pass

            self._subscribed_tokens = new_tokens

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._task:
            self._task.cancel()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ws_price_stream] connection error: {e!r}", flush=True)
            self._connected = False
            if not self._running:
                break
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 1.5, RECONNECT_MAX_DELAY_SEC
            )

    async def _connect_and_listen(self) -> None:
        print(
            f"[ws_price_stream] connecting to Polymarket WS "
            f"(tokens: {len(self._subscribed_tokens)})...",
            flush=True,
        )
        async with websockets.connect(
            POLYMARKET_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            ssl=_ssl_context_for_polymarket_ws(),
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = RECONNECT_DELAY_SEC
            print("[ws_price_stream] connected!", flush=True)

            if self._subscribed_tokens:
                sub_msg = json.dumps({
                    "assets_ids": list(self._subscribed_tokens),
                    "type": "market",
                    "custom_feature_enabled": True,
                })
                await ws.send(sub_msg)

            self._ping_task = asyncio.create_task(self._ping_loop(ws))
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(ws))
            # אתחול שעון הודעות בזמן ההתחברות כך ש-watchdog לא יסגור מיידית.
            self._last_msg_ts = time.time()

            try:
                async for raw in ws:
                    self._last_msg_ts = time.time()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    txt = raw.strip()
                    if txt == "PONG" or txt == "pong":
                        continue
                    try:
                        msgs = json.loads(txt)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(msgs, dict):
                        msgs = [msgs]
                    elif not isinstance(msgs, list):
                        continue

                    for msg in msgs:
                        await self._handle_message(msg)
            finally:
                if self._ping_task:
                    self._ping_task.cancel()
                if self._watchdog_task:
                    self._watchdog_task.cancel()
                self._ws = None
                self._connected = False

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                try:
                    await ws.send("PING")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _watchdog_loop(self, ws: Any) -> None:
        """אם אין הודעות > STALE_RECONNECT_SEC — סוגרים את ה-WS להכריח reconnect.

        חשוב במיוחד כאשר ה-TCP חי אבל הצד השני "שותק" (half-open), מצב שבו
        קריאת `async for raw in ws` לא תקפוץ לבד וה-strategy יראה מחירים stale.
        """
        try:
            while True:
                await asyncio.sleep(5)
                age = time.time() - self._last_msg_ts if self._last_msg_ts > 0 else 0
                if age > STALE_RECONNECT_SEC:
                    print(
                        f"[ws_price_stream] watchdog: no messages for {age:.1f}s — forcing reconnect",
                        flush=True,
                    )
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type") or msg.get("type") or ""
        asset_id = msg.get("asset_id") or msg.get("market") or ""

        if not asset_id and "changes" in msg:
            for change in msg.get("changes", []):
                aid = change.get("asset_id") or ""
                if aid and aid in self._prices:
                    asset_id = aid
                    break

        if asset_id not in self._prices:
            if asset_id:
                return
            for tid in self._subscribed_tokens:
                if tid in str(msg):
                    asset_id = tid
                    break
            if asset_id not in self._prices:
                return

        tp = self._prices[asset_id]
        changed = False

        if event_type in ("book", "initial"):
            changed = tp.update_from_book(msg)
        elif event_type == "price_change":
            changed = tp.update_from_price_change(msg)
        elif event_type == "best_bid_ask":
            changed = tp.update_from_best_bid_ask(msg)
        elif event_type == "last_trade_price":
            pass
        else:
            if "bids" in msg or "asks" in msg:
                changed = tp.update_from_book(msg)
            elif "changes" in msg:
                changed = tp.update_from_best_bid_ask(msg)

        if changed:
            self._broadcast_to_frontend(asset_id, tp)
            for cb in self._on_price_change_callbacks:
                try:
                    cb(asset_id, tp)
                except Exception:
                    pass


price_stream = PriceStreamManager()
