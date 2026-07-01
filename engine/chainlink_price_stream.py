"""
Chainlink Data Stream price feed — the EXACT source Polymarket resolves BTC
"Up or Down 5m" markets on (resolutionSource: data.chain.link/streams/btc-usd).

WebSocket: wss://ws-live-data.polymarket.com
Subscribe: {"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink",
            "type":"*","filters":"{\\"symbol\\":\\"btc/usd\\"}"}]}

EMPIRICAL BEHAVIOUR (probed against the live feed, June 2026):
  * A `subscribe` is answered with ONE snapshot frame — a ~60s rolling window of
    1 Hz ticks — shaped {topic,type,timestamp, payload:{symbol,"data":[{timestamp<ms>,
    value},...]}}. Feed latency of the newest tick is ~1-2s (same as the Polymarket UI).
  * There is NO server push after the snapshot. To keep the price fresh we
    RE-SUBSCRIBE on the same socket every POLL_INTERVAL_SEC — each re-subscribe
    returns a fresh snapshot whose newest tick advances in lockstep with wall time.
  * A `PING` text frame must be sent periodically to keep the connection alive.

Because each snapshot only reaches back ~60s, a boundary tick (the Price to Beat)
is only recoverable if we were connected at/before the window opened. We therefore
keep a rolling tick buffer AND an immutable per-window cache: once a window's
boundary tick is captured it survives buffer loss (reconnects). If the engine
cold-starts mid-window we CANNOT know that window's Price to Beat from the live
feed — get_price_to_beat() returns None (the caller marks it "not certain" and
falls back), and the next window is captured exactly.

This module mirrors the connection-quality patterns of ws_price_stream.py
(reconnect backoff, PING loop, stale watchdog) but is a SEPARATE connection to a
SEPARATE endpoint (that one is the CLOB order-book feed; this one is prices).
"""
from __future__ import annotations

import asyncio
import json
import math
import ssl
import time
from typing import Any, Optional

import websockets
import websockets.exceptions

CHAINLINK_WS_URL = "wss://ws-live-data.polymarket.com"
CHAINLINK_TOPIC = "crypto_prices_chainlink"
CHAINLINK_SYMBOL = "btc/usd"

# חובה לפי התיעוד — PING טקסט לשמירת החיבור. הפיד לא שולח push; אנחנו re-subscribe כדי לרענן.
PING_INTERVAL_SEC = 5.0
POLL_INTERVAL_SEC = 1.0
RECONNECT_DELAY_SEC = 1.0
RECONNECT_MAX_DELAY_SEC = 15.0
# אם לא הגיע snapshot בזמן הזה — סוגרים ומתחברים מחדש (half-open defence).
STALE_RECONNECT_SEC = 20.0
# מעל הגיל הזה (לפי חותמת הטיק עצמה) המחיר "הנוכחי" נחשב לא-טרי — צרכן עושה fallback.
FRESH_PRICE_MAX_AGE_SEC = 5.0
# חוצץ טיקים מתגלגל — חייב להחזיק חלון 15 דק׳ מלא (900 טיקים @1Hz) + מרווח, כדי שהטיק
# בגבול החלון לא ייגזם באמצע חלון 15m לפני שנצרך (~4KB בזיכרון — זניח).
BUFFER_MAX_TICKS = 1024
# רצפת סבירות למחיר BTC — דוחה NaN/Inf/0/שלילי/ערכים אבסורדיים לפני שהם נכנסים לחוצץ
# (מראה את btc_price.fetch_chainlink_btc_usd_polygon_latest שדורש px > 1000). קריטי כי
# ה-cache של Price to Beat הוא immutable — טיק פגום בגבול היה מרעיל את כל החלון.
MIN_PLAUSIBLE_BTC_USD = 1000.0
# הטיק הראשון שגדול/שווה ל-window_start חייב ליפול בתוך החלון הזה מפתיחת החלון; אחרת
# יש פער חשוד בגבול ואנחנו לא סומכים על הערך (מחזירים None).
PTB_BOUNDARY_GRACE_SEC = 5.0
PTB_CACHE_MAX = 512


def _ssl_context() -> ssl.SSLContext:
    """ב-macOS/Python לעיתים חסר bundle של CA — certifi (תלות httpx) פותר SSLCertVerificationError."""
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def _subscribe_message() -> str:
    """הצורה היחידה שהתקבלה אמפירית (filters חייב להיות מחרוזת JSON)."""
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [{
            "topic": CHAINLINK_TOPIC,
            "type": "*",
            "filters": json.dumps({"symbol": CHAINLINK_SYMBOL}),
        }],
    })


class TickBuffer:
    """חוצץ טיקים טהור (ts_ms -> value), ממויין מרומזת דרך המפתחות. חסר רשת — נבדק ביחידה."""

    def __init__(self, max_ticks: int = BUFFER_MAX_TICKS) -> None:
        self.max_ticks = max_ticks
        self._ticks: dict[int, float] = {}

    def ingest(self, data: Any) -> int:
        """מוסיף טיקים מרשימת {"timestamp":<ms>,"value":<num>}; dedup לפי ts; גוזם ישנים.

        מחזיר כמה טיקים *חדשים* נוספו. רשומות פגומות מדולגות; אף פעם לא זורק.
        """
        if not isinstance(data, list):
            return 0
        added = 0
        for d in data:
            if not isinstance(d, dict):
                continue
            ts = d.get("timestamp")
            val = d.get("value")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            v = float(val)
            # דוחה NaN/Inf/0/שלילי/מתחת לרצפה — לעולם לא לאחסן מחיר לא-אמיתי (מגן על ה-cache
            # ה-immutable של Price to Beat וגם על מחיר "נוכחי").
            if not math.isfinite(v) or v <= MIN_PLAUSIBLE_BTC_USD:
                continue
            ts_i = int(ts)
            if ts_i not in self._ticks:
                added += 1
            self._ticks[ts_i] = v
        if len(self._ticks) > self.max_ticks:
            self._prune()
        return added

    def _prune(self) -> None:
        excess = len(self._ticks) - self.max_ticks
        if excess > 0:
            for k in sorted(self._ticks.keys())[:excess]:
                self._ticks.pop(k, None)

    def size(self) -> int:
        return len(self._ticks)

    def latest(self) -> Optional[tuple[int, float]]:
        if not self._ticks:
            return None
        ts = max(self._ticks)
        return ts, self._ticks[ts]

    def earliest_ts_ms(self) -> Optional[int]:
        if not self._ticks:
            return None
        return min(self._ticks)

    def price_to_beat(
        self, window_start_sec: int, grace_sec: float = PTB_BOUNDARY_GRACE_SEC
    ) -> Optional[tuple[int, float]]:
        """Price to Beat = הטיק הראשון מהפיד שחותמתו >= window_start.

        מוחזר (ts_ms, value) רק אם אנחנו *סומכים* עליו:
          1. יש כיסוי בגבול/לפניו (הטיק הכי מוקדם בחוצץ <= window_start) — אחרת נכנסנו
             באמצע חלון ואין לנו את הטיק האמיתי → None.
          2. הטיק הראשון >= window_start נופל בתוך grace מפתיחת החלון — אחרת יש פער
             חשוד בגבול → None.
        """
        if not self._ticks:
            return None
        ws_ms = int(window_start_sec) * 1000
        if min(self._ticks) > ws_ms:
            # אין כיסוי בגבול או לפניו — לא ניתן לדעת את הטיק האמיתי.
            return None
        candidates = [t for t in self._ticks if t >= ws_ms]
        if not candidates:
            # הטיק בגבול עדיין לא הגיע לפיד (החלון רק נפתח).
            return None
        boundary_ts = min(candidates)
        if boundary_ts > ws_ms + int(grace_sec * 1000):
            return None
        return boundary_ts, self._ticks[boundary_ts]


class ChainlinkPriceStream:
    """מנהל חיבור מתמשך לפיד Chainlink של Polymarket עם polling ע"י re-subscribe."""

    def __init__(self) -> None:
        self._buffer = TickBuffer()
        # cache חד-חד-ערכי לפי window_start — immutable אחרי לכידה, שורד reconnect/buffer loss.
        self._ptb_cache: dict[int, tuple[int, float]] = {}
        self._ws: Any = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._running = False
        self._reconnect_delay = RECONNECT_DELAY_SEC
        self._connected = False
        self._last_msg_ts: float = 0.0

    # ── properties ──────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_ts(self) -> float:
        return self._last_msg_ts

    def is_fresh(self, max_age_sec: float = FRESH_PRICE_MAX_AGE_SEC) -> bool:
        """האם הפיד חי וקיבל snapshot לאחרונה — ללוגיקת fallback ל-Binance."""
        if not self._connected or self._last_msg_ts <= 0:
            return False
        return (time.time() - self._last_msg_ts) <= max_age_sec

    # ── reads (used by main.py / consumers) ─────────────────────────────────
    def get_current_price(
        self, max_age_sec: float = FRESH_PRICE_MAX_AGE_SEC
    ) -> Optional[dict[str, Any]]:
        """מחיר BTC נוכחי מהטיק הטרי ביותר — או None אם stale (צרכן יעשה fallback).

        הגיל נמדד לפי חותמת הטיק (זמן הפיד), כך שגם אם נשארנו מחוברים אך הפיד קפא,
        המחיר ייחשב stale ולא יוגש כ"נוכחי".
        """
        lt = self._buffer.latest()
        if lt is None:
            return None
        ts_ms, val = lt
        age = time.time() - ts_ms / 1000.0
        if age > max_age_sec:
            return None
        return {"value": val, "ts_ms": ts_ms, "age_sec": age}

    def _get_ptb_entry(self, window_start_sec: int) -> Optional[tuple[int, float]]:
        """מחזיר (ts_ms, value) של ה-Price to Beat — מ-cache או מחושב-ונשמר. מקור אמת יחיד.

        קורא מהמשתנה המקומי ולא חוזר לקרוא מה-cache אחרי prune (מניעת KeyError אם ה-prune
        פינה את המפתח שזה עתה הוכנס — למשל שאילתא לחלון ישן כשה-cache מלא בחדשים).
        """
        ws = int(window_start_sec)
        cached = self._ptb_cache.get(ws)
        if cached is not None:
            return cached
        res = self._buffer.price_to_beat(ws)
        if res is None:
            return None
        self._ptb_cache[ws] = res
        self._prune_ptb_cache()
        return res

    def get_price_to_beat(self, window_start_sec: int) -> Optional[float]:
        """Price to Beat של החלון — מדויק כמו Polymarket, או None אם לא ניתן ללכוד.

        cache immutable פר-חלון: אחרי לכידה הערך קבוע לתמיד (שורד reconnect).
        """
        entry = self._get_ptb_entry(window_start_sec)
        return None if entry is None else entry[1]

    def get_price_to_beat_full(self, window_start_sec: int) -> Optional[dict[str, Any]]:
        """כמו get_price_to_beat אך מחזיר גם את חותמת הטיק — לצרכי דיבוג/אימות."""
        entry = self._get_ptb_entry(window_start_sec)
        if entry is None:
            return None
        ts_ms, val = entry
        return {"value": val, "tick_ts_ms": ts_ms, "window_start": int(window_start_sec), "exact": True}

    def is_midwindow_gap(self, window_start_sec: int) -> bool:
        """True רק כשנכנסנו *באמצע חלון* (אין כיסוי בגבול/לפניו) — לא כשהטיק בגבול פשוט עוד
        לא הגיע לפיד (~1-2ש׳ בתחילת כל חלון). מבדיל בין cold-start אמיתי לבין מצב מעבר תקין.
        """
        if self.get_price_to_beat(window_start_sec) is not None:
            return False
        earliest = self._buffer.earliest_ts_ms()
        return earliest is not None and earliest > int(window_start_sec) * 1000

    def _prune_ptb_cache(self) -> None:
        excess = len(self._ptb_cache) - PTB_CACHE_MAX
        if excess > 0:
            for k in sorted(self._ptb_cache.keys())[:excess]:
                self._ptb_cache.pop(k, None)

    # ── message ingestion (pure-ish; unit tested) ───────────────────────────
    def _ingest_message(self, msg: Any) -> bool:
        """מעבד frame יחיד מהפיד. מחזיר True אם נוספו טיקים חדשים."""
        if not isinstance(msg, dict):
            return False
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return False
        data = payload.get("data")
        if not isinstance(data, list):
            return False
        added = self._buffer.ingest(data)
        # מעדכנים את שעון הטריות רק כשהגיעו טיקים *חדשים* — כך פיד "מהדהד אך קפוא"
        # (מחזיר snapshot זהה) ייחשב stale, ה-watchdog יבצע reconnect, ו-is_fresh יאמר אמת.
        if added > 0:
            self._last_msg_ts = time.time()
        return added > 0

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self._running = False
        for t in (self._ping_task, self._poll_task, self._watchdog_task, self._task):
            if t:
                t.cancel()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[chainlink_stream] connection error: {e!r}", flush=True)
            self._connected = False
            if not self._running:
                break
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 1.5, RECONNECT_MAX_DELAY_SEC
            )

    async def _connect_and_listen(self) -> None:
        print("[chainlink_stream] connecting to Polymarket Chainlink feed...", flush=True)
        async with websockets.connect(
            CHAINLINK_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            ssl=_ssl_context(),
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = RECONNECT_DELAY_SEC
            self._last_msg_ts = time.time()
            print("[chainlink_stream] connected!", flush=True)

            await ws.send(_subscribe_message())
            self._ping_task = asyncio.create_task(self._ping_loop(ws))
            self._poll_task = asyncio.create_task(self._poll_loop(ws))
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(ws))

            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    txt = raw.strip()
                    if not txt or txt in ("PONG", "pong"):
                        continue
                    try:
                        # parse_constant מנטרל NaN/Infinity/-Infinity → None (json מפרש אותם
                        # כברירת מחדל ל-float('nan')/inf); ה-ingest ידחה None, כך שערך לא-סופי
                        # לעולם לא מגיע לחוצץ (הגנה בשכבות יחד עם רצפת הסבירות).
                        msg = json.loads(txt, parse_constant=lambda _c: None)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, list):
                        for m in msg:
                            self._ingest_message(m)
                    else:
                        self._ingest_message(msg)
            finally:
                for t in (self._ping_task, self._poll_task, self._watchdog_task):
                    if t:
                        t.cancel()
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

    async def _poll_loop(self, ws: Any) -> None:
        """הפיד לא שולח push — אנחנו re-subscribe כדי לקבל snapshot טרי בכל POLL_INTERVAL_SEC."""
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                try:
                    await ws.send(_subscribe_message())
                except Exception:
                    break
                self._warm_current_boundaries()
        except asyncio.CancelledError:
            pass

    def _warm_current_boundaries(self) -> None:
        """לוכד ל-cache את ה-Price to Beat של החלונות הפעילים (5m ו-15m) ברגע שהטיק בגבול
        זמין — כך ה-cache ה-immutable מתמלא גם בלי צרכן HTTP (למשל כשה-dashboard סגור),
        לפני שהטיק ייגזם מהחוצץ המתגלגל. פעולה מקומית זולה (dict lookup + סריקת חוצץ)."""
        try:
            now = int(time.time())
            for step in (300, 900):
                self.get_price_to_beat(now - (now % step))
        except Exception:
            pass

    async def _watchdog_loop(self, ws: Any) -> None:
        """אם לא הגיע snapshot > STALE_RECONNECT_SEC — סוגרים כדי להכריח reconnect (half-open)."""
        try:
            while True:
                await asyncio.sleep(5)
                age = time.time() - self._last_msg_ts if self._last_msg_ts > 0 else 0
                if age > STALE_RECONNECT_SEC:
                    print(
                        f"[chainlink_stream] watchdog: no snapshot for {age:.1f}s — forcing reconnect",
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


# singleton בתהליך — נצרך ע"י main.py ו-btc_price.py (כמו price_stream ב-ws_price_stream).
chainlink_stream = ChainlinkPriceStream()
