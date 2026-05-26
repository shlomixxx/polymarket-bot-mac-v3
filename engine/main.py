"""
שרת API למנוע הבוט — Electron מתחבר ל-localhost.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional


def _load_dotenv_from_project_root() -> None:
    """טוען משתנים מקובץ .env בשורש הפרויקט (ליד package.json).

    לא דורס ערכים שכבר הוגדרו ב-shell / Electron — כך ש־export ידני עדיין גובר.
    ללא תלות ב־python-dotenv (פורס שורות KEY=VAL ו־# הערות).
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


_load_dotenv_from_project_root()

import httpx
from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from atomic_io import atomic_write_text
from btc_price import (
    PriceHistoryBuffer,
    fetch_btc_spot_usdt,
    fetch_chainlink_btc_usd_polygon_at_window_start,
    fetch_open_price_at_window_start,
    fetch_window_start_end_btc_usd,
)
from demo_engine import DemoEngine
from market_discovery import (
    discover_active_btc_window,
    discovery_warmer_loop,
    get_clob_book,
    peek_window_timing_for_ui,
    seconds_until_window_end,
)
from request_logger import init_request_logger, log_event as log_request_event, make_request_id
from run_logging import (
    append_event,
    append_strategy_journal,
    log_run_dir,
    periodic_snapshot_loop,
    write_engine_startup,
    write_strategy_journal_header,
    write_strategy_snapshot,
)
from live_clob import (
    fetch_live_portfolio,
    fetch_polymarket_clob_account,
    place_entry_order as live_place_entry_order,
    place_limit_order as live_place_limit_order,
    reset_portfolio_cache,
)
import secret_store
from order_validation import validate_contracts_for_market
from pricing_limits import MAX_LEGIT_SHARE_PRICE_USD, MIN_LEGIT_SHARE_PRICE_USD
from signal_engine import CONFIDENCE_THRESHOLD, compute_signals
from history_tracker import (
    record_window_result,
    get_recent_windows,
    get_hourly_breakdown,
    get_last_window_winners,
)
from trigger_engine import TriggerEngine, TriggerConfig
from strategy_runner import StrategyConfig, StrategyRunner
from tips_v2 import delete_run_folder_by_key, generate_tips_v2, list_run_folders_detailed
from ws_price_stream import price_stream
from analytics.api_routes import router as analytics_router
from analytics.db_migration import ensure_analytics_tables, migrate_json_to_sqlite

def _autoload_private_key_from_store() -> None:
    """טוען מפתח שמור (Keychain / Secret Service / Credential Manager) אל os.environ
    אם אין עדיין מפתח בסביבה. קורה ברגע import של המודול כך ש-run-bot/npm run dev
    אף פעם לא דורשים להקליד את המפתח מחדש אחרי שמירה חד־פעמית."""
    try:
        existing = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
        if existing:
            return
        stored = secret_store.load_key()
        if stored:
            os.environ["POLYMARKET_PRIVATE_KEY"] = stored
    except Exception:
        # אף פעם לא מפילים את המנוע בגלל אחסון מקומי
        pass


_autoload_private_key_from_store()

demo = DemoEngine()
runner = StrategyRunner(demo)
trigger = TriggerEngine()
trigger.inject(demo)


def _live_trades_for_tips_v2() -> list[dict[str, Any]]:
    """עסקאות חיות למיזוג בניתוח v3 — רק מהסשן הנוכחי אחרי איפוס (לא שובר היסטוריה בדיסק)."""
    raw = list(demo.state.trades) if demo.state.trades else []
    ts0 = getattr(demo.state, "stats_epoch_ts", None)
    if ts0 is None:
        return raw
    t0 = float(ts0)
    return [t for t in raw if float(t.get("ts") or 0) >= t0]

# ── UI runtime timer (uptime) ────────────────────────────────────────────────
# זהו "זמן ריצה" ל-UI: מהרגע שהמנוע עלה או מהאיפוס האחרון שבוצע דרך ה-API.
_ui_runtime_started_ts: float = time.time()
_ui_runtime_reason: str = "engine_start"
# שווי נטו (דמו) ברגע תחילת ריצת ה-UI — לחישוב רווח מצטבר מול אותה נקודת זמן
_ui_runtime_equity_baseline_usd: float = float(demo.equity_snapshot_usd())


def _reset_ui_runtime(reason: str) -> None:
    global _ui_runtime_started_ts, _ui_runtime_reason, _ui_runtime_equity_baseline_usd
    _ui_runtime_started_ts = time.time()
    _ui_runtime_reason = reason
    try:
        _ui_runtime_equity_baseline_usd = float(demo.equity_snapshot_usd())
    except Exception:
        _ui_runtime_equity_baseline_usd = float(demo.state.balance_usd)


# סשן "ריצת בוט" לשידור: מרגע הפעלת semi/auto עד כיבוי — זמן ריצה, PnL מצטבר, win rate
_bot_run_started_ts: Optional[float] = None
_bot_run_equity_baseline_usd: Optional[float] = None


def _start_bot_run_session() -> None:
    global _bot_run_started_ts, _bot_run_equity_baseline_usd
    _bot_run_started_ts = time.time()
    try:
        _bot_run_equity_baseline_usd = float(demo.equity_snapshot_usd())
    except Exception:
        _bot_run_equity_baseline_usd = float(demo.state.balance_usd)


def _clear_bot_run_session() -> None:
    global _bot_run_started_ts, _bot_run_equity_baseline_usd
    _bot_run_started_ts = None
    _bot_run_equity_baseline_usd = None


def _ensure_bot_run_session_if_active() -> None:
    """אם semi/auto/trigger פעיל אבל אין סשן — מתחילים סשן עכשיו."""
    if runner.rt.mode == "off" and not getattr(trigger.config, "active", False):
        return
    global _bot_run_started_ts
    if _bot_run_started_ts is None:
        _start_bot_run_session()


def _any_engine_active() -> bool:
    """True if either the strategy runner or the quick-trade trigger engine is running."""
    return runner.rt.mode != "off" or bool(getattr(trigger.config, "active", False))


def _bot_run_win_rate_stats() -> dict[str, Any]:
    """אחוז ניצחונות ביציאות ממומשות מתחילת סשן הבוט (כמו חישוב win rate בלשונית סטטיסטיקה)."""
    t0 = _bot_run_started_ts
    if t0 is None:
        return {
            "bot_run_win_rate_pct": None,
            "bot_run_exit_trades_n": 0,
            "bot_run_wins_n": 0,
        }
    t0f = float(t0)
    exits: list[dict[str, Any]] = []
    for t in demo.state.trades or []:
        if float(t.get("ts") or 0) < t0f:
            continue
        rp = t.get("realized_pnl")
        if rp is None:
            continue
        try:
            float(rp)
        except (TypeError, ValueError):
            continue
        typ = t.get("type") or ""
        styp = str(typ)
        # פוזיציות שנטענו מה-chain דרך reconcile (reconcile_origin) לא נפתחו ב-BUY של
        # הריצה — להוציא אותן מסטטיסטיקת win_rate כדי שלא יעוותו את המדד של הריצה.
        if t.get("reconcile_origin"):
            continue
        if (
            typ == "EXPIRE_0"
            or typ in ("SETTLE_WIN", "SETTLE_LOSS", "SETTLE_UNKNOWN")
            or styp.startswith("SELL")
        ):
            exits.append(t)
    n = len(exits)
    if n == 0:
        return {
            "bot_run_win_rate_pct": None,
            "bot_run_exit_trades_n": 0,
            "bot_run_wins_n": 0,
        }
    wins = sum(1 for x in exits if float(x.get("realized_pnl") or 0) > 0)
    wr = 100.0 * wins / n
    return {
        "bot_run_win_rate_pct": round(wr, 2),
        "bot_run_exit_trades_n": n,
        "bot_run_wins_n": wins,
    }


DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(Path(__file__).resolve().parent))).resolve()
DATA_ROOT.mkdir(parents=True, exist_ok=True)

CONFIG_PERSISTED_PATH = DATA_ROOT / "config_persisted.json"
TRIGGER_CONFIG_PERSISTED_PATH = DATA_ROOT / "trigger_config_persisted.json"


def _save_trigger_config() -> None:
    """שומר הגדרות טריגר לדיסק."""
    try:
        from dataclasses import asdict
        data = asdict(trigger.config)
        data.pop("active", None)  # לא שומרים active — תמיד מתחיל כבוי (אלא אם auto_start)
        atomic_write_text(
            TRIGGER_CONFIG_PERSISTED_PATH,
            json.dumps(data, ensure_ascii=False, indent=2),
        )
    except Exception as e:
        print(f"[polymarket-bot] אזהרה: לא ניתן לשמור trigger config — {e}", flush=True)


def _load_trigger_config() -> None:
    """טוען הגדרות טריגר מהפעלה קודמת."""
    if not TRIGGER_CONFIG_PERSISTED_PATH.exists():
        return
    try:
        data = json.loads(TRIGGER_CONFIG_PERSISTED_PATH.read_text(encoding="utf-8"))
        c = trigger.config
        for k, v in data.items():
            if k == "active":
                continue  # active נקבע לפי auto_start בהמשך
            if hasattr(c, k):
                setattr(c, k, v)
        # אם auto_start=True, מפעילים אוטומטית
        if getattr(c, "auto_start", False):
            # אם רצו לנהל את ההפעלה ידנית (למשל דרך run-bot-with-logs.command),
            # ניתן לנטרל auto_start באמצעות משתנה סביבה.
            if os.environ.get("POLYMARKET_BOT_DISABLE_AUTO_START") in ("1", "true", "yes"):
                trigger.status = "כבוי (auto_start מנוטרל)"
            else:
                c.active = True
                trigger.status = "הופעל אוטומטית"
                # מבטיח שדף השידור יציג נתונים מיד עם עליית התהליך.
                if _bot_run_started_ts is None:
                    _start_bot_run_session()
    except Exception as e:
        print(f"[polymarket-bot] אזהרה: לא ניתן לטעון trigger config — {e}", flush=True)


def _load_persisted_config() -> None:
    """טוען הגדרות שמורות מהפעלה קודמת."""
    if not CONFIG_PERSISTED_PATH.exists():
        return
    try:
        data = json.loads(CONFIG_PERSISTED_PATH.read_text(encoding="utf-8"))
        c = runner.rt.config
        for k, v in data.items():
            # לא משחזרים mode מהדיסק: בכל הפעלה של התוכנה/מנוע מתחילים ב־"כבוי"
            # עד שהמשתמש מפעיל ידנית (חצי־אוטומטי / אוטומטי מלא). mode עדיין נשמר לקובץ לעיון.
            if k == "mode":
                continue
            # live_trading נשמר ב-runtime, לא ב-config — נטפל בנפרד למטה.
            if k == "live_trading":
                continue
            if hasattr(c, k):
                setattr(c, k, v)
        # שחזור מצב "כסף אמיתי" מהממשק (אם נשמר). לא תלוי ב-POLYMARKET_LIVE env.
        if "live_trading" in data:
            try:
                runner.rt.live_trading = bool(data.get("live_trading"))
            except Exception:
                runner.rt.live_trading = False
    except Exception as e:
        print(f"[polymarket-bot] אזהרה: לא ניתן לטעון config שמור — {e}", flush=True)


async def _clamp_min_contracts_to_market_floor() -> None:
    """מגדיל את min_contracts לפחות למינימום השוק הפעיל (מ־discover + CLOB) לפני שמירה לדיסק."""
    try:
        m = await discover_active_btc_window(runner.rt.config.btc_window)
        if not m:
            return
        floor = int(math.ceil(float(m.order_min_size)))
        cur = int(getattr(runner.rt.config, "min_contracts", 1))
        if floor > cur:
            runner.rt.config.min_contracts = floor
    except Exception:
        pass


def _save_persisted_config() -> None:
    """שומר הגדרות + מצב לקובץ — לזכירה בהפעלות הבאות."""
    try:
        c = runner.rt.config
        data: dict[str, Any] = {
            "investment_usd": c.investment_usd,
            "entry_price_cents": c.entry_price_cents,
            "min_contracts": c.min_contracts,
            "btc_window": getattr(c, "btc_window", "5m"),
            "take_profit_pct": c.take_profit_pct,
            "min_minutes_for_entry": c.min_minutes_for_entry,
            "freeze_last_minutes": c.freeze_last_minutes,
            "intermediate_block_new_entries": c.intermediate_block_new_entries,
            "dca_enabled": c.dca_enabled,
            "dca_slices": c.dca_slices,
            "dca_interval_sec": c.dca_interval_sec,
            "dca_discount_enabled": c.dca_discount_enabled,
            "dca_discount_pct": c.dca_discount_pct,
            "hedge_enabled": c.hedge_enabled,
            "hedge_combined_ask_max": c.hedge_combined_ask_max,
            "side_preference": c.side_preference,
            "auto_reenter_after_tp": c.auto_reenter_after_tp,
            "reenter_cooldown_sec": c.reenter_cooldown_sec,
            "max_entries_per_window": c.max_entries_per_window,
            "max_notional_per_window_usd": c.max_notional_per_window_usd,
            "max_trades_per_hour": c.max_trades_per_hour,
            "near_entry_pct": c.near_entry_pct,
            "near_tp_pct": c.near_tp_pct,
            "dca_tp_override_pct": c.dca_tp_override_pct,
            "book_log_interval_sec": getattr(c, "book_log_interval_sec", 0.0),
            "loss_recovery_enabled": getattr(c, "loss_recovery_enabled", False),
            "loss_recovery_step_pct": getattr(c, "loss_recovery_step_pct", 20.0),
            "loss_recovery_every_n_losses": getattr(c, "loss_recovery_every_n_losses", 1),
            "loss_recovery_max_multiplier": getattr(c, "loss_recovery_max_multiplier", 10.0),
            "order_mode": getattr(c, "order_mode", "limit"),
            "entry_slippage_pct": getattr(c, "entry_slippage_pct", 2.0),
            "exit_slippage_pct": getattr(c, "exit_slippage_pct", 5.0),
            "peak_watchdog_enabled": getattr(c, "peak_watchdog_enabled", True),
            "peak_retreat_exit_pct": getattr(c, "peak_retreat_exit_pct", 2.0),
            "retry_max_attempts": getattr(c, "retry_max_attempts", 3),
            "hold_to_resolution_enabled": bool(getattr(c, "hold_to_resolution_enabled", False)),
            "hold_to_resolution_min_dca_slices": int(getattr(c, "hold_to_resolution_min_dca_slices", 2)),
            "hold_to_resolution_min_price": float(getattr(c, "hold_to_resolution_min_price", 0.85)),
            "hold_to_resolution_stop_loss_enabled": bool(getattr(c, "hold_to_resolution_stop_loss_enabled", True)),
            "investment_mode": str(getattr(c, "investment_mode", "fixed")),
            "investment_pct_of_portfolio": float(getattr(c, "investment_pct_of_portfolio", 5.0)),
            "follow_last_winner_enabled": bool(getattr(c, "follow_last_winner_enabled", False)),
            "follow_last_winner_lookback": int(getattr(c, "follow_last_winner_lookback", 1)),
            "follow_last_winner_mode": str(getattr(c, "follow_last_winner_mode", "forward")),
            "follow_last_winner_min_btc_drift_pct": float(getattr(c, "follow_last_winner_min_btc_drift_pct", 0.0)),
            "mode": runner.rt.mode,
            # מצב "כסף אמיתי" נשלט מהממשק — נשמר בין הפעלות אבל נגדר ע"י POLYMARKET_LIVE env בפריסה.
            "live_trading": bool(getattr(runner.rt, "live_trading", False)),
        }
        atomic_write_text(CONFIG_PERSISTED_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[polymarket-bot] אזהרה: לא ניתן לשמור config — {e}", flush=True)
_snapshot_task: Optional[asyncio.Task] = None
_history_recorder_task: Optional[asyncio.Task] = None
_ws_subscription_task: Optional[asyncio.Task] = None
_discovery_warmer_task: Optional[asyncio.Task] = None


async def _supervise(name: str, coro_factory, restart_delay: float = 3.0) -> None:
    """עוטף לולאת רקע ב-restart loop. אם הלולאה קורסת — חוזרים אחרי delay במקום ליפול בשקט.

    coro_factory הוא callable שמחזיר coroutine חדש בכל ריצה (כי coroutine נצרך פעם אחת).
    """
    while True:
        try:
            await coro_factory()
            print(f"[supervise] {name} exited cleanly", flush=True)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(
                f"[supervise] {name} crashed: {e!r} — restarting in {restart_delay}s",
                flush=True,
            )
            try:
                append_event("background_task_crash", {"name": name, "error": str(e)[:200]})
            except Exception:
                pass
            try:
                await asyncio.sleep(restart_delay)
            except asyncio.CancelledError:
                raise


async def _ws_subscription_loop(interval: float = 5.0) -> None:
    """Keeps WebSocket subscriptions in sync with the active market tokens."""
    last_up = ""
    last_down = ""
    await asyncio.sleep(0.5)
    while True:
        try:
            m = await discover_active_btc_window(runner.rt.config.btc_window)
            if m and (m.token_up != last_up or m.token_down != last_down):
                await price_stream.subscribe_tokens(
                    m.token_up, m.token_down,
                    token_side_map={m.token_up: "Up", m.token_down: "Down"},
                )
                last_up, last_down = m.token_up, m.token_down
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval)
price_buf = PriceHistoryBuffer()
last_epoch_for_open: int = 0
cached_open: Optional[float] = None
cached_ptb_source: str = "binance_1m"

# ── Auto History Recorder state ──────────────────────────────────────────────
_last_recorded_epoch: int = 0  # epoch of the last window we recorded


async def auto_history_recorder_loop(interval_sec: float = 10.0) -> None:
    """
    רץ ברקע — מזהה מתי חלון נסגר ומתעד אוטומטית מי ניצח.

    לוגיקה:
    1. מביא את החלון הפעיל כל interval_sec שניות.
    2. אם ה-epoch השתנה → חלון קודם נסגר.
    3. מביא מחיר פתיחה של החלון הקודם + מחיר BTC נוכחי כמחיר סגירה.
    4. מחשב מי ניצח (Up אם close > open, אחרת Down).
    5. שומר ב-history_tracker.
    """
    global _last_recorded_epoch
    prev_epoch: int = 0
    prev_slug: str = ""
    prev_window_sec: int = 300

    await asyncio.sleep(1.5)  # המתנה קצרה לאחר עלייה

    while True:
        try:
            m = await discover_active_btc_window()
            if m is None:
                await asyncio.sleep(interval_sec)
                continue

            current_epoch = m.epoch
            current_slug = m.slug
            current_window_sec = m.window_sec

            if prev_epoch == 0:
                # אתחול ראשוני — שמור epoch נוכחי, אל תרשום
                prev_epoch = current_epoch
                prev_slug = current_slug
                prev_window_sec = current_window_sec
                _last_recorded_epoch = current_epoch
                await asyncio.sleep(interval_sec)
                continue

            # חלון חדש זוהה — נרשום את הקודם
            if current_epoch != prev_epoch and prev_epoch != _last_recorded_epoch:
                try:
                    btc_open = await fetch_open_price_at_window_start(prev_epoch)
                    btc_close = await fetch_btc_spot_usdt()

                    side_won: Optional[str] = None
                    if btc_open is not None and btc_close is not None:
                        side_won = "Up" if btc_close > btc_open else "Down"

                    record_window_result(
                        epoch=prev_epoch,
                        slug=prev_slug,
                        window_sec=prev_window_sec,
                        side_won=side_won,
                        btc_open=btc_open,
                        btc_close=btc_close,
                    )
                    _last_recorded_epoch = prev_epoch
                    print(
                        f"[history] חלון {prev_slug} נרשם: {side_won} "
                        f"(open={btc_open:.2f}, close={btc_close:.2f})",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"[history] שגיאה בתיעוד חלון {prev_slug}: {exc}", flush=True)

            prev_epoch = current_epoch
            prev_slug = current_slug
            prev_window_sec = current_window_sec

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[history] שגיאה בלולאה: {exc}", flush=True)

        await asyncio.sleep(interval_sec)
TIPS_V2_CACHE_PATH = DATA_ROOT / "tips_v2_cache.json"
TIPS_V2_CACHE_TTL_SEC = 300.0  # cache for 5 minutes
WEB_DIST_DIR = Path(os.environ.get("WEB_DIST_DIR", str(Path(__file__).resolve().parent.parent / "dist"))).resolve()
ORDERBOOK_SUMMARY_CACHE_TTL_SEC = float(
    os.environ.get("ORDERBOOK_CACHE_TTL", "0.5")
)
last_orderbook_summary: Optional[dict[str, Any]] = None
last_orderbook_summary_ts: float = 0.0
# Lock למניעת race condition: מונע שני threads מלעדכן את ה-cache בו-זמנית
_orderbook_summary_lock: Optional[asyncio.Lock] = None
_shared_httpx: Optional[httpx.AsyncClient] = None


def _get_orderbook_lock() -> asyncio.Lock:
    """יוצר Lock בפעם הראשונה שנקרא (חייב לרוץ בתוך event loop)."""
    global _orderbook_summary_lock
    if _orderbook_summary_lock is None:
        _orderbook_summary_lock = asyncio.Lock()
    return _orderbook_summary_lock


def _get_shared_httpx() -> httpx.AsyncClient:
    """מחזיר httpx client משותף — חוסך TLS handshake בכל בקשה."""
    global _shared_httpx
    if _shared_httpx is None:
        _shared_httpx = httpx.AsyncClient(timeout=8.0)
    return _shared_httpx


def _ensure_log_run_dir() -> None:
    """אם LOG_RUN_DIR לא הוגדר — יוצר תיקיית לוגים ברירת מחדל (תמיד לוגים)."""
    if os.environ.get("LOG_RUN_DIR"):
        log_dir = Path(os.environ["LOG_RUN_DIR"])
        print(f"[polymarket-bot] לוגים: {log_dir}", flush=True)
        return
    root = DATA_ROOT
    day = time.strftime("%Y-%m-%d")
    t = time.strftime("%H-%M-%S")
    run_dir = root / "logs" / "runs" / day / t
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["LOG_RUN_DIR"] = str(run_dir)
    print(f"[polymarket-bot] לוגים: {run_dir}", flush=True)
    meta = {
        "schema": "polymarket-bot-run-meta/v1",
        "run_name": f"polymarket-bot {day} {t}",
        "run_date": day,
        "run_time": t,
        "started_at_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "auto_created": True,
    }
    with open(run_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    (run_dir / "combined.log").touch()


def _count_strategy_run_dirs() -> int:
    """כמה תיקיות ריצה עם snapshot יש תחת DATA_ROOT (קלט ל־ניתוח v3)."""
    root = DATA_ROOT / "logs" / "runs"
    if not root.is_dir():
        return 0
    n = 0
    try:
        for day_dir in root.iterdir():
            if not day_dir.is_dir():
                continue
            for run_dir in day_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                if (run_dir / "strategy_snapshot.json").is_file():
                    n += 1
    except OSError:
        return 0
    return n


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _snapshot_task, _history_recorder_task, _ws_subscription_task, _discovery_warmer_task
    _ensure_log_run_dir()
    n_snap = _count_strategy_run_dirs()
    print(
        f"[polymarket-bot] DATA_ROOT={DATA_ROOT} | ריצות עם snapshot לניתוח v3: {n_snap} "
        f"(ב-Railway: Volume בנתיב /data או DATA_ROOT זהה — אחרת הנתונים נמחקים בכל deploy)",
        flush=True,
    )
    _load_persisted_config()
    _load_trigger_config()
    _reset_ui_runtime("lifespan_start")
    runner.rt.mode = "off"
    # Start real-time WebSocket price stream from Polymarket CLOB
    price_stream.start()
    _ws_subscription_task = asyncio.create_task(
        _supervise("ws_subscription_loop", lambda: _ws_subscription_loop(5.0))
    )
    # רענון רקע של גילוי שוק — דואג שהקאש לא יזדקן ושפניות UI לא ימתינו ל־Gamma איטי.
    _discovery_warmer_task = asyncio.create_task(
        _supervise(
            "discovery_warmer_loop",
            lambda: discovery_warmer_loop(lambda: runner.rt.config.btc_window, interval_sec=10.0),
        )
    )
    runner.start_loop()
    trigger.start_loop()
    _history_recorder_task = asyncio.create_task(
        _supervise("auto_history_recorder_loop", lambda: auto_history_recorder_loop(10.0))
    )
    if log_run_dir():
        write_engine_startup(runner, demo)
        write_strategy_journal_header(runner, demo)
        runner.rt.log_listeners.append(append_strategy_journal)
        append_event("lifespan_start", {"log_run_dir": str(log_run_dir())})
        _snapshot_task = asyncio.create_task(
            _supervise("periodic_snapshot_loop", lambda: periodic_snapshot_loop(runner, demo, 60.0))
        )
    yield
    if _snapshot_task:
        _snapshot_task.cancel()
        try:
            await _snapshot_task
        except asyncio.CancelledError:
            pass
        _snapshot_task = None
    if _ws_subscription_task:
        _ws_subscription_task.cancel()
        try:
            await _ws_subscription_task
        except asyncio.CancelledError:
            pass
        _ws_subscription_task = None
    if _history_recorder_task:
        _history_recorder_task.cancel()
        try:
            await _history_recorder_task
        except asyncio.CancelledError:
            pass
        _history_recorder_task = None
    if _discovery_warmer_task:
        _discovery_warmer_task.cancel()
        try:
            await _discovery_warmer_task
        except asyncio.CancelledError:
            pass
        _discovery_warmer_task = None
    price_stream.stop()
    trigger.stop_loop()
    runner.stop_loop()
    if _shared_httpx is not None:
        await _shared_httpx.aclose()


app = FastAPI(title="Polymarket Bot Engine", lifespan=lifespan)
# לא משלבים allow_origins=["*"] עם allow_credentials=True — הדפדפן לא מקבל
# Access-Control-Allow-Origin בקריאות cross-origin (למשל Vite :5175 → API :8767).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logger: writes every HTTP request to engine/logs/requests.jsonl.
# Disable with LOG_REQUESTS=0. Also exposes /api/_log/client-request for the frontend.
init_request_logger(app)

# ── V3 Analytics ────────────────────────────────────────────────────────────
app.include_router(analytics_router)
# Auto-create analytics tables on startup; migration runs on first /api/analytics/migrate call
try:
    ensure_analytics_tables()
except Exception as _e:
    print(f"[polymarket-bot] analytics tables init warning: {_e}", flush=True)


@app.get("/api/health")
async def health_get():
    return {"ok": True}


@app.head("/api/health")
async def health_head():
    # מאפשר ל-wait-on לקבל 200 גם על HEAD
    return {}


@app.get("/api/market/current")
async def market_current():
    # ‎discover_active_btc_window כבר מבצע wait_for פנימי של 8s + stale-on-error fallback.
    # ה־wait_for החיצוני (10s) הוא שכבת בטיחות נוספת בלבד למקרה שהלוק הפנימי תקוע.
    try:
        m = await asyncio.wait_for(
            discover_active_btc_window(runner.rt.config.btc_window), timeout=10.0
        )
    except asyncio.TimeoutError:
        m = None
    if not m:
        raise HTTPException(503, "שוק פעיל לא זמין כרגע — נסה שוב בעוד מספר שניות")
    global last_epoch_for_open, cached_open, cached_ptb_source
    if m.epoch != last_epoch_for_open:
        last_epoch_for_open = m.epoch
        # קודם Binance (מהיר ויציב) כדי שלא לחסום את ה-endpoint; שדרוג ל-Chainlink ברקע אם יזמין.
        try:
            cached_open = await asyncio.wait_for(
                fetch_open_price_at_window_start(m.epoch), timeout=3.0
            )
            cached_ptb_source = "binance_1m_fallback"
        except Exception:
            cached_open = None
            cached_ptb_source = "binance_1m_fallback"
        # שדרוג Chainlink ברקע — לא מעכב את התשובה; אם יחזור ערך, יוחל בקריאה הבאה.
        async def _upgrade_to_chainlink(epoch: int) -> None:
            global cached_open, cached_ptb_source, last_epoch_for_open
            try:
                ptb = await asyncio.wait_for(
                    fetch_chainlink_btc_usd_polygon_at_window_start(epoch), timeout=10.0
                )
            except Exception:
                return
            if ptb is not None and last_epoch_for_open == epoch:
                cached_open = ptb
                cached_ptb_source = "chainlink_polygon_window"
        asyncio.create_task(_upgrade_to_chainlink(m.epoch))
    note_chainlink = (
        "ייחוס: סיבוב Chainlink BTC/USD על Polygon שעודכן עד לפתיחת החלון (קרוב לפיד האונ־צ׳יין). "
        "באתר Polymarket מוצג לעיתים Chainlink Data Streams — עשוי להסטות סנטים/דולרים בודדים. "
        "BTC חי במסך מהמנוע הוא Binance spot."
    )
    note_binance = (
        "ייחוס מ-Binance (נר 1m בפתיחת החלון) — כשהפיד של Chainlink על Polygon לא זמין; "
        "Polymarket רשמית מסתמך על Chainlink Streams — עלול להסטות מהאתר."
    )
    return {
        "slug": m.slug,
        "epoch": m.epoch,
        "title": m.title,
        "token_up": m.token_up,
        "token_down": m.token_down,
        "outcome_prices": list(m.outcome_prices),
        "order_min_size": m.order_min_size,
        "order_min_size_source": getattr(m, "order_min_size_source", "gamma"),
        "window_sec": m.window_sec,
        "btc_window": runner.rt.config.btc_window,
        "seconds_left": seconds_until_window_end(m.epoch, m.window_sec),
        "price_to_beat": cached_open,
        "price_to_beat_source": cached_ptb_source,
        "price_to_beat_note": note_chainlink if cached_ptb_source == "chainlink_polygon_window" else note_binance,
        # מ-Gamma API — אין שם מחיר BTC מספרי; רק קישור למקור הרזולוציה
        "polymarket_resolution_source": m.resolution_source,
    }


@app.get("/api/btc/live")
async def btc_live():
    try:
        p = await fetch_btc_spot_usdt()
    except Exception as e:
        raise HTTPException(502, str(e))
    price_buf.add(p)
    return {"price": p, "history": [{"t": a, "p": b} for a, b in price_buf.points]}


@app.get("/api/btc/window-prices")
async def btc_window_prices(epoch: int, window_sec: int = 300):
    """מחירי פתיחה/סוף חלון (פרוקסי Binance) — לתצוגה רטרואקטיבית כשחסרים בשמירת העסקה."""
    if window_sec <= 0:
        raise HTTPException(400, "window_sec must be positive")
    try:
        return await fetch_window_start_end_btc_usd(epoch, window_sec)
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/orderbook/{token_id}")
async def orderbook(token_id: str):
    try:
        return await get_clob_book(_get_shared_httpx(), token_id)
    except Exception as e:
        raise HTTPException(502, str(e))


def _best_book_prices(book: Optional[dict[str, Any]]) -> dict[str, Any]:
    """מחזיר bid/ask/mid מספר ה-orderbook; מוחזר {"bid":None,"ask":None,"mid":None} אם ריק."""
    if not book:
        return {"bid": None, "ask": None, "mid": None}
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid = float(bids[0]["price"]) if bids else None
    ask = float(asks[0]["price"]) if asks else None
    mid: Optional[float] = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2.0
    elif bid is not None:
        mid = bid
    elif ask is not None:
        mid = ask
    return {"bid": bid, "ask": ask, "mid": mid}


@app.get("/api/market/orderbook-summary")
async def market_orderbook_summary():
    """סיכום מחירי חוזה (bid/ask/mid) לצד Up ו-Down לשוק BTC Up/Down הפעיל (לפי הגדרת btc_window)."""
    global last_orderbook_summary, last_orderbook_summary_ts
    try:
        m = await asyncio.wait_for(
            discover_active_btc_window(runner.rt.config.btc_window), timeout=10.0
        )
    except asyncio.TimeoutError:
        m = None
    if m is None:
        # נחזיר את הקאש האחרון אם יש (גם אם פג ה-TTL) — עדיף על שגיאה ל-UI.
        if last_orderbook_summary is not None:
            stale = dict(last_orderbook_summary)
            stale["source"] = "stale"
            return stale
        raise HTTPException(503, "שוק פעיל לא זמין כרגע — נסה שוב בעוד מספר שניות")

    now = time.time()
    # Try WebSocket cache first — zero-latency response
    up_tp = price_stream.get_price(m.token_up)
    down_tp = price_stream.get_price(m.token_down)
    if (
        up_tp is not None and (up_tp.bid is not None or up_tp.ask is not None) and (now - up_tp.ts) < 15.0
        and down_tp is not None and (down_tp.bid is not None or down_tp.ask is not None) and (now - down_tp.ts) < 15.0
    ):
        ws_result: dict[str, Any] = {
            "slug": m.slug,
            "up": {"bid": up_tp.bid, "ask": up_tp.ask, "mid": up_tp.mid},
            "down": {"bid": down_tp.bid, "ask": down_tp.ask, "mid": down_tp.mid},
            "source": "ws",
        }
        last_orderbook_summary = dict(ws_result)
        last_orderbook_summary_ts = now
        return ws_result

    if (
        last_orderbook_summary is not None
        and (now - last_orderbook_summary_ts) <= ORDERBOOK_SUMMARY_CACHE_TTL_SEC
        and last_orderbook_summary.get("slug") == m.slug
    ):
        return dict(last_orderbook_summary)

    # lock לעדכון ה-cache: מונע race condition בין בקשות מקביליות
    async with _get_orderbook_lock():
        # בדיקה כפולה אחרי קבלת ה-lock — אולי מישהו כבר עדכן
        now = time.time()
        if (
            last_orderbook_summary is not None
            and (now - last_orderbook_summary_ts) <= ORDERBOOK_SUMMARY_CACHE_TTL_SEC
            and last_orderbook_summary.get("slug") == m.slug
        ):
            return dict(last_orderbook_summary)

        up_book: Optional[dict[str, Any]] = None
        down_book: Optional[dict[str, Any]] = None
        degraded_reason: Optional[str] = None
        client = _get_shared_httpx()
        try:
            # בקשות Up/Down במקביל — חוסכות חצי מזמן ה-round-trip ומונעות timeout.
            # עטיפת asyncio.wait_for קובעת תקרה אחידה כך שה-endpoint לא יחרוג מעבר ל-10s
            # (לקוח ב-UI מחכה עד 15s — נותר מרווח להחזרת fallback מהקאש).
            up_book, down_book = await asyncio.wait_for(
                asyncio.gather(
                    get_clob_book(client, m.token_up),
                    get_clob_book(client, m.token_down),
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            degraded_reason = "clob_timeout"
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            degraded_reason = f"clob_http_{status}" if status is not None else "clob_http_error"
            if status == 429:
                degraded_reason = "clob_rate_limited"
        except Exception:
            degraded_reason = "clob_unavailable"

        result: dict[str, Any] = {
            "slug": m.slug,
            "up": _best_book_prices(up_book),
            "down": _best_book_prices(down_book),
        }
        if degraded_reason:
            result["degraded"] = True
            result["degraded_reason"] = degraded_reason
            # Rate-limit/שגיאת CLOB: נחזיר snapshot קודם אם יש כדי לא לשבור UI
            if last_orderbook_summary and last_orderbook_summary.get("slug") == m.slug:
                fallback = dict(last_orderbook_summary)
                fallback["degraded"] = True
                fallback["degraded_reason"] = degraded_reason
                fallback["stale"] = True
                # מעדכנים את חותמת הזמן גם בכשל — כך בקשות מקביליות שיכנסו תוך ה-TTL
                # יקבלו fallback מהיר במקום לשחזר מחדש את הקריאה האיטית ל-CLOB
                last_orderbook_summary_ts = now
                return fallback
            # אין cache קודם — נשמור תוצאה ריקה לזמן קצר כדי למנוע thundering herd
            last_orderbook_summary_ts = now
        else:
            last_orderbook_summary = dict(result)
            last_orderbook_summary_ts = now
        return result


@app.get("/api/demo/state")
async def demo_state():
    # נסמן equity תמיד — כך last_mark.unrealized_usd מתאפס מיד כשאין פוזיציות
    await demo.mark_to_market()
    _ensure_bot_run_session_if_active()
    out = demo.state.to_dict()
    # נשלח עם state כדי שדפי שידור/OBS לא יסמכו רק על /api/strategy/config או /api/runtime
    out["ui_runtime_equity_baseline_usd"] = float(_ui_runtime_equity_baseline_usd)
    out["bot_run_started_ts"] = _bot_run_started_ts
    out["bot_run_equity_baseline_usd"] = _bot_run_equity_baseline_usd
    out.update(_bot_run_win_rate_stats())
    return out


@app.get("/api/demo/snapshot")
async def demo_snapshot():
    """Lightweight snapshot — balance + last_mark + positions + recent trades/equity slices.
    Designed for fast 500ms polling: keeps P&L display fresh and lets the broadcast chart
    and LAST TRADES appear within 500ms of a page refresh (without heavy CLOB calls)."""
    s = demo.state
    bw = runner.rt.config.btc_window
    if bw not in ("5m", "15m"):
        bw = "5m"
    market_timing = peek_window_timing_for_ui(bw)
    return {
        "balance_usd": s.balance_usd,
        "positions": [
            {
                "token_id": p.token_id,
                "side": p.side,
                "contracts": p.contracts,
                "avg_cost": p.avg_cost,
            }
            for p in s.positions
        ],
        "last_mark": s.last_mark,
        "trades": s.trades[-400:],
        "equity_history": s.equity_history[-2000:],
        "bot_run_started_ts": _bot_run_started_ts,
        "bot_run_equity_baseline_usd": _bot_run_equity_baseline_usd,
        "ui_runtime_equity_baseline_usd": float(_ui_runtime_equity_baseline_usd),
        "bot_run_win_rate_pct": _bot_run_win_rate_stats().get("bot_run_win_rate_pct"),
        "bot_run_exit_trades_n": _bot_run_win_rate_stats().get("bot_run_exit_trades_n"),
        "bot_run_wins_n": _bot_run_win_rate_stats().get("bot_run_wins_n"),
        "market_timing": market_timing,
    }


class ResetBody(BaseModel):
    balance: float = 10_000.0


@app.post("/api/demo/reset")
async def demo_reset(body: ResetBody):
    demo.reset(body.balance)
    runner.sync_runtime_after_demo_positions_cleared()
    _reset_ui_runtime("demo_reset")
    if not _any_engine_active():
        _clear_bot_run_session()
    else:
        _start_bot_run_session()
    append_event("demo_reset", {"balance": body.balance})
    write_strategy_snapshot(runner, demo)
    return {"ok": True}


@app.post("/api/demo/clear-stats")
async def demo_clear_stats():
    await demo.reset_stats_and_flatten_positions()
    runner.sync_runtime_after_demo_positions_cleared()
    _reset_ui_runtime("demo_clear_stats")
    if not _any_engine_active():
        _clear_bot_run_session()
    else:
        _start_bot_run_session()
    append_event("demo_clear_stats", {})
    write_strategy_snapshot(runner, demo)
    return {"ok": True}


@app.get("/api/runtime")
async def api_runtime():
    """זמן ריצה ל-UI: מהרגע שהמנוע עלה או מהאיפוס האחרון."""
    now = time.time()
    started_ts = float(_ui_runtime_started_ts)
    return {
        "started_ts": started_ts,
        "uptime_sec": max(0.0, now - started_ts),
        "reason": _ui_runtime_reason,
        "now_ts": now,
        "equity_baseline_usd": float(_ui_runtime_equity_baseline_usd),
    }


@app.get("/api/demo/export.csv", response_class=PlainTextResponse)
async def demo_export_csv(live_only: bool = False):
    """CSV של עסקאות + snapshot mark-to-market (נוח לאקסל). live_only=1 — רק עסקאות חי."""
    await demo.mark_to_market()
    fn = "live-trades.csv" if live_only else "demo-trades.csv"
    return PlainTextResponse(
        demo.export_csv(live_only=live_only),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


class TradeBody(BaseModel):
    side: str
    token_id: str
    contracts: float
    limit_price: Optional[float] = None
    order_min_size: float = 5.0


@app.post("/api/demo/trade")
async def demo_trade(body: TradeBody):
    if body.side not in ("Up", "Down"):
        raise HTTPException(400, "צד חייב Up או Down")
    r = await demo.simulate_market_buy(
        body.side,
        body.token_id,
        body.contracts,
        body.limit_price,
        context={"order_min_size": float(body.order_min_size)},
    )
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "כשל"))
    return r


class ConfigBody(BaseModel):
    investment_usd: float = 5.0
    entry_price_cents: float = 20.0
    min_contracts: int = 5
    btc_window: str = "5m"  # 5m | 15m — שוק Polymarket (לא מספר חוזים)
    take_profit_pct: float = 20.0
    min_minutes_for_entry: float = 3.0
    freeze_last_minutes: float = 1.0
    intermediate_block_new_entries: bool = True
    dca_enabled: bool = False
    dca_slices: int = 4
    dca_interval_sec: float = 30.0
    dca_discount_enabled: bool = False
    dca_discount_pct: float = 2.0
    hedge_enabled: bool = False
    hedge_combined_ask_max: float = 0.98
    side_preference: str = "Up"
    auto_reenter_after_tp: bool = True
    reenter_cooldown_sec: float = 8.0
    max_entries_per_window: int = 3
    max_notional_per_window_usd: float = 1_000_000.0
    max_trades_per_hour: int = 1_000
    near_entry_pct: float = 3.0
    near_tp_pct: float = 2.0
    dca_tp_override_pct: float = 50.0
    book_log_interval_sec: float = 0.0
    loss_recovery_enabled: bool = False
    loss_recovery_step_pct: float = 20.0
    loss_recovery_every_n_losses: int = 1
    loss_recovery_max_multiplier: float = 10.0
    # ביצוע מובטח: "limit" (ברירת מחדל, תאימות לאחור) או "market" (FOK/FAK)
    order_mode: str = "limit"
    entry_slippage_pct: float = 2.0
    exit_slippage_pct: float = 5.0
    peak_watchdog_enabled: bool = True
    peak_retreat_exit_pct: float = 2.0
    retry_max_attempts: int = 3
    hold_to_resolution_enabled: bool = False
    hold_to_resolution_min_dca_slices: int = 2
    hold_to_resolution_min_price: float = 0.85
    hold_to_resolution_stop_loss_enabled: bool = True
    investment_mode: str = "fixed"
    investment_pct_of_portfolio: float = 5.0
    # Follow Last Winner (FLW)
    follow_last_winner_enabled: bool = False
    follow_last_winner_lookback: int = 1
    follow_last_winner_mode: str = "forward"
    follow_last_winner_min_btc_drift_pct: float = 0.0


@app.post("/api/strategy/config")
async def strategy_config(body: ConfigBody):
    if body.btc_window not in ("5m", "15m"):
        raise HTTPException(400, "btc_window must be 5m or 15m")
    # entry_price_cents → דולרים לחוזה; Polymarket טיפוסית 0.01–0.99$
    ep = float(body.entry_price_cents)
    if ep < MIN_LEGIT_SHARE_PRICE_USD * 100 or ep > MAX_LEGIT_SHARE_PRICE_USD * 100:
        raise HTTPException(
            400,
            f"entry_price_cents must be between {MIN_LEGIT_SHARE_PRICE_USD * 100:.0f} and {MAX_LEGIT_SHARE_PRICE_USD * 100:.0f} (0.01–0.99$ per share)",
        )
    if body.side_preference not in ("Up", "Down", "signal"):
        raise HTTPException(400, "side_preference")
    if int(body.loss_recovery_every_n_losses) < 1:
        raise HTTPException(400, "loss_recovery_every_n_losses must be >= 1")
    if float(body.loss_recovery_max_multiplier) < 1.0:
        raise HTTPException(400, "loss_recovery_max_multiplier must be >= 1")
    if body.order_mode not in ("limit", "market"):
        raise HTTPException(400, "order_mode must be 'limit' or 'market'")
    if float(body.entry_slippage_pct) < 0 or float(body.entry_slippage_pct) > 50:
        raise HTTPException(400, "entry_slippage_pct must be between 0 and 50")
    if float(body.exit_slippage_pct) < 0 or float(body.exit_slippage_pct) > 50:
        raise HTTPException(400, "exit_slippage_pct must be between 0 and 50")
    if float(body.peak_retreat_exit_pct) < 0 or float(body.peak_retreat_exit_pct) > 50:
        raise HTTPException(400, "peak_retreat_exit_pct must be between 0 and 50")
    if int(body.retry_max_attempts) < 0 or int(body.retry_max_attempts) > 10:
        raise HTTPException(400, "retry_max_attempts must be between 0 and 10")
    if body.investment_mode not in ("fixed", "percent"):
        raise HTTPException(400, "investment_mode must be 'fixed' or 'percent'")
    if float(body.investment_pct_of_portfolio) < 0 or float(body.investment_pct_of_portfolio) > 100:
        raise HTTPException(400, "investment_pct_of_portfolio must be between 0 and 100")
    if int(body.hold_to_resolution_min_dca_slices) < 0:
        raise HTTPException(400, "hold_to_resolution_min_dca_slices must be >= 0")
    if float(body.hold_to_resolution_min_price) < 0 or float(body.hold_to_resolution_min_price) > 1:
        raise HTTPException(400, "hold_to_resolution_min_price must be between 0 and 1")
    if int(body.follow_last_winner_lookback) < 1 or int(body.follow_last_winner_lookback) > 5:
        raise HTTPException(400, "follow_last_winner_lookback must be between 1 and 5")
    if body.follow_last_winner_mode not in ("forward", "reverse"):
        raise HTTPException(400, "follow_last_winner_mode must be 'forward' or 'reverse'")
    if float(body.follow_last_winner_min_btc_drift_pct) < 0 or float(body.follow_last_winner_min_btc_drift_pct) > 10:
        raise HTTPException(400, "follow_last_winner_min_btc_drift_pct must be between 0 and 10")
    c = runner.rt.config
    for k, v in body.model_dump().items():
        if hasattr(c, k):
            setattr(c, k, v)
    c.side_preference = body.side_preference  # type: ignore
    await _clamp_min_contracts_to_market_floor()
    saved = {**body.model_dump(), "min_contracts": runner.rt.config.min_contracts}
    append_event(
        "strategy_config_updated",
        {"config": saved, "mode": runner.rt.mode},
    )
    append_strategy_journal(f"\n--- עדכון אסטרטגיה: {time.strftime('%H:%M:%S')} ---\n")
    write_strategy_snapshot(runner, demo)
    _save_persisted_config()
    return {"ok": True, "config": saved}


@app.get("/api/strategy/config")
async def get_strategy_config():
    _ensure_bot_run_session_if_active()
    c = runner.rt.config
    return {
        "investment_usd": c.investment_usd,
        "entry_price_cents": c.entry_price_cents,
        "min_contracts": c.min_contracts,
        "btc_window": getattr(c, "btc_window", "5m"),
        "take_profit_pct": c.take_profit_pct,
        "min_minutes_for_entry": c.min_minutes_for_entry,
        "freeze_last_minutes": c.freeze_last_minutes,
        "intermediate_block_new_entries": c.intermediate_block_new_entries,
        "dca_enabled": c.dca_enabled,
        "dca_slices": c.dca_slices,
        "dca_interval_sec": c.dca_interval_sec,
        "dca_discount_enabled": c.dca_discount_enabled,
        "dca_discount_pct": c.dca_discount_pct,
        "hedge_enabled": c.hedge_enabled,
        "hedge_combined_ask_max": c.hedge_combined_ask_max,
        "side_preference": c.side_preference,
        "auto_reenter_after_tp": c.auto_reenter_after_tp,
        "reenter_cooldown_sec": c.reenter_cooldown_sec,
        "max_entries_per_window": c.max_entries_per_window,
        "max_notional_per_window_usd": c.max_notional_per_window_usd,
        "max_trades_per_hour": c.max_trades_per_hour,
        "near_entry_pct": c.near_entry_pct,
        "near_tp_pct": c.near_tp_pct,
        "dca_tp_override_pct": c.dca_tp_override_pct,
        "book_log_interval_sec": getattr(c, "book_log_interval_sec", 0.0),
        "loss_recovery_enabled": getattr(c, "loss_recovery_enabled", False),
        "loss_recovery_step_pct": getattr(c, "loss_recovery_step_pct", 20.0),
        "loss_recovery_every_n_losses": getattr(c, "loss_recovery_every_n_losses", 1),
        "loss_recovery_max_multiplier": getattr(c, "loss_recovery_max_multiplier", 10.0),
        "loss_recovery_streak": demo.state.loss_recovery_streak,
        "loss_recovery_multiplier": demo.state.loss_recovery_multiplier,
        "order_mode": getattr(c, "order_mode", "limit"),
        "entry_slippage_pct": getattr(c, "entry_slippage_pct", 2.0),
        "exit_slippage_pct": getattr(c, "exit_slippage_pct", 5.0),
        "peak_watchdog_enabled": getattr(c, "peak_watchdog_enabled", True),
        "peak_retreat_exit_pct": getattr(c, "peak_retreat_exit_pct", 2.0),
        "retry_max_attempts": getattr(c, "retry_max_attempts", 3),
        "hold_to_resolution_enabled": bool(getattr(c, "hold_to_resolution_enabled", False)),
        "hold_to_resolution_min_dca_slices": int(getattr(c, "hold_to_resolution_min_dca_slices", 2)),
        "hold_to_resolution_min_price": float(getattr(c, "hold_to_resolution_min_price", 0.85)),
        "hold_to_resolution_stop_loss_enabled": bool(getattr(c, "hold_to_resolution_stop_loss_enabled", True)),
        "investment_mode": str(getattr(c, "investment_mode", "fixed")),
        "investment_pct_of_portfolio": float(getattr(c, "investment_pct_of_portfolio", 5.0)),
        "follow_last_winner_enabled": bool(getattr(c, "follow_last_winner_enabled", False)),
        "follow_last_winner_lookback": int(getattr(c, "follow_last_winner_lookback", 1)),
        "follow_last_winner_mode": str(getattr(c, "follow_last_winner_mode", "forward")),
        "follow_last_winner_min_btc_drift_pct": float(getattr(c, "follow_last_winner_min_btc_drift_pct", 0.0)),
        "mode": runner.rt.mode,
        "last_status": runner.rt.last_status,
        # מפתח אחרון מ-status() — נוח למיפוי אנגלי בדף שידור/OBS
        "strategy_status_key": getattr(runner.rt, "_last_status_key", "") or "",
        "last_tick_ts": runner.rt.last_tick_ts,
        # זמן ריצה ל-UI (מסונכרן עם /api/runtime)
        "ui_runtime_started_ts": float(_ui_runtime_started_ts),
        "ui_runtime_uptime_sec": max(0.0, time.time() - float(_ui_runtime_started_ts)),
        "ui_runtime_equity_baseline_usd": float(_ui_runtime_equity_baseline_usd),
        # שידור: זמן מהכניסה הראשונה בלולאת האסטרטגיה (None עד כניסה ראשונה)
        "strategy_first_buy_ts": runner.rt.strategy_first_buy_ts,
        "bot_run_started_ts": _bot_run_started_ts,
        "bot_run_equity_baseline_usd": _bot_run_equity_baseline_usd,
        **_bot_run_win_rate_stats(),
    }


class ModeBody(BaseModel):
    mode: str  # off | semi | auto


@app.post("/api/strategy/mode")
async def strategy_mode(body: ModeBody):
    if body.mode not in ("off", "semi", "auto"):
        raise HTTPException(400, "mode")
    prev = runner.rt.mode
    runner.rt.mode = body.mode  # type: ignore
    if body.mode == "off":
        runner.rt.strategy_first_buy_ts = None
        # שומר את הסשן אם "מסחר מהיר" עדיין פעיל — דף השידור צריך להמשיך להציג נתונים.
        if not getattr(trigger.config, "active", False):
            _clear_bot_run_session()
    elif prev == "off":
        if _bot_run_started_ts is None:
            _start_bot_run_session()
    if body.mode == "off" and prev != "off":
        _reset_ui_runtime("strategy_mode_off")
    # כשעוברים מ-off למצב פעיל: מנקים יומן כדי שה-UI יציג "היסטוריה חדשה"
    if prev == "off" and body.mode != "off":
        runner.rt.log_lines = []
        runner.rt.log_entries = []
        runner.rt.pending_approval = None
        runner.rt.last_status = ""
        runner.rt._last_status_key = ""
        runner.rt.last_tick_ts = 0.0
    append_event("strategy_mode_changed", {"previous": prev, "mode": body.mode})
    append_strategy_journal(f"\n--- שינוי מצב: {prev} → {body.mode} ({time.strftime('%H:%M:%S')}) ---\n")
    write_strategy_snapshot(runner, demo)
    _save_persisted_config()
    return {"ok": True, "mode": body.mode}


@app.get("/api/logs/run-dir")
async def api_logs_run_dir():
    """מחזיר את נתיב תיקיית הלוגים של הריצה הנוכחית (אם הוגדר LOG_RUN_DIR)."""
    p = log_run_dir()
    return {"log_run_dir": str(p) if p else None, "active": bool(p)}


@app.get("/api/strategy/logs")
async def strategy_logs():
    return {"lines": runner.rt.log_lines[-100:]}


@app.get("/api/strategy/log-entries")
async def strategy_log_entries():
    """רשומות יומן מובנות: אירועים וסטטוסים, עם session_id לקישור למסכל."""
    return {"entries": runner.rt.log_entries[-300:]}


@app.get("/api/strategy/tips-v2")
async def strategy_tips_v2(max_runs: int = 50, min_samples: int = 50, use_guardrails: bool = True):
    try:
        max_runs_i = max(1, min(int(max_runs), 200))
        min_samples_i = max(10, min(int(min_samples), 10000))
    except Exception:
        max_runs_i = 50
        min_samples_i = 50

    try:
        n_demo_trades = len(_live_trades_for_tips_v2())
    except Exception:
        n_demo_trades = 0
    try:
        sepoch = getattr(demo.state, "stats_epoch_ts", None)
    except Exception:
        sepoch = None
    cache_key = {
        "max_runs": max_runs_i,
        "min_samples": min_samples_i,
        "use_guardrails": bool(use_guardrails),
        "v": 3,
        "demo_trades_n": n_demo_trades,
        "stats_epoch_ts": sepoch,
    }
    now = time.time()
    if TIPS_V2_CACHE_PATH.exists():
        try:
            cached = json.loads(TIPS_V2_CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("cache_key") == cache_key and (now - float(cached.get("cached_at") or 0)) <= TIPS_V2_CACHE_TTL_SEC:
                return JSONResponse(content=cached.get("data") or {}, media_type="application/json; charset=utf-8")
        except Exception:
            pass

    from dataclasses import asdict

    current_cfg = asdict(runner.rt.config)
    live_trades = _live_trades_for_tips_v2()
    data = generate_tips_v2(
        max_runs=max_runs_i,
        min_samples=min_samples_i,
        use_guardrails=bool(use_guardrails),
        current_cfg=current_cfg,
        live_trades=live_trades,
    )
    try:
        atomic_write_text(
            TIPS_V2_CACHE_PATH,
            json.dumps({"cached_at": now, "cache_key": cache_key, "data": data}, ensure_ascii=False, indent=2),
        )
    except Exception:
        pass
    return JSONResponse(content=data, media_type="application/json; charset=utf-8")


class TipsV2DeleteRunBody(BaseModel):
    run_key: str


@app.get("/api/strategy/tips-v2/runs")
async def strategy_tips_v2_runs(limit: int = 3000):
    """רשימת תיקיות ריצה + קבצים — לניהול נתוני ניתוח v3 (כל הריצות עד תקרת limit)."""
    try:
        lim = max(1, min(int(limit), 10000))
    except Exception:
        lim = 3000
    return list_run_folders_detailed(max_folders=lim)


@app.post("/api/strategy/tips-v2/delete-run")
async def strategy_tips_v2_delete_run(body: TipsV2DeleteRunBody):
    """מחיקת תיקיית ריצה שלמה (תאריך/שעה) מתחת ל־DATA_ROOT/logs/runs."""
    ok, msg = delete_run_folder_by_key(body.run_key)
    if not ok:
        raise HTTPException(400, msg)
    try:
        TIPS_V2_CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "detail": msg}


@app.get("/api/strategy/pending")
async def strategy_pending():
    return {"pending": runner.rt.pending_approval}


class ApproveBody(BaseModel):
    # None = השתמש במצב הזמן־אמת של המנוע (מופעל מהממשק). True/False — כפייה לבקשה בודדת.
    live: Optional[bool] = None


@app.post("/api/strategy/approve")
async def strategy_approve(body: ApproveBody = Body(default=ApproveBody())):
    return await runner.approve_pending(live=body.live)


@app.post("/api/strategy/reject")
async def strategy_reject():
    await runner.reject_pending()
    return {"ok": True}


@app.post("/api/live/private-key")
async def set_private_key(body: dict[str, Any]):
    """שומר מפתח פרטי ל-Polymarket CLOB.
    persist=false (ברירת מחדל): סשן זיכרון בלבד — חייבים להקליד מחדש בכל הרצה.
    persist=true: נשמר גם ב-Keychain/Secret Service/Credential Manager של המחשב
    המקומי כך שהרצות הבאות יטענו אוטומטית בלי הקלדה חוזרת."""
    k = (body.get("key") or "").strip()
    persist = bool(body.get("persist") or False)

    os.environ["POLYMARKET_PRIVATE_KEY"] = k
    reset_portfolio_cache()

    persisted_ok = False
    if persist:
        if k:
            persisted_ok = secret_store.save_key(k)
        else:
            # "שמירה ריקה לצמיתות" = מחיקה מפורשת של מפתח שמור
            secret_store.delete_key()
        _invalidate_persisted_key_cache()

    clob_ok = False
    try:
        import py_clob_client.client  # noqa: F401

        clob_ok = True
    except ImportError:
        pass
    return {
        "ok": True,
        "set": bool(k),
        "persist_requested": persist,
        "persisted": persisted_ok,
        "persisted_in_keychain": secret_store.has_persisted_key(),
        "py_clob_client_installed": clob_ok,
    }


@app.delete("/api/live/private-key")
async def delete_private_key():
    """מוחק מפתח מהסשן ומה-Keychain. אחרי זה צריך להקליד שוב כדי לסחור לייב."""
    os.environ["POLYMARKET_PRIVATE_KEY"] = ""
    removed = secret_store.delete_key()
    _invalidate_persisted_key_cache()
    reset_portfolio_cache()
    return {"ok": True, "removed_from_keychain": removed, **_live_mode_state()}


_persisted_key_cache: Optional[bool] = None
_persisted_key_cache_ts: float = 0.0
_PERSISTED_KEY_CACHE_TTL = 10.0  # בודקים keyring פעם ב-10 שניות, לא בכל בקשה


def _invalidate_persisted_key_cache() -> None:
    global _persisted_key_cache, _persisted_key_cache_ts
    _persisted_key_cache = None
    _persisted_key_cache_ts = 0.0


def _live_mode_state() -> dict[str, Any]:
    """מחזיר את מצב "כסף אמיתי" הכולל — דגל ממשק + kill-switch פריסה + מפתח."""
    global _persisted_key_cache, _persisted_key_cache_ts
    env_kill = os.environ.get("POLYMARKET_LIVE", "").strip().lower() in (
        "0", "false", "no", "off",
    )
    has_key = bool((os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip())
    enabled = bool(getattr(runner.rt, "live_trading", False))
    # effective = בפועל ישלח פקודות לייב
    effective = enabled and (not env_kill) and has_key
    reason = None
    if enabled and env_kill:
        reason = "POLYMARKET_LIVE=0 (kill-switch בפריסה)"
    elif enabled and not has_key:
        reason = "חסר POLYMARKET_PRIVATE_KEY"
    now = time.time()
    if _persisted_key_cache is None or (now - _persisted_key_cache_ts) > _PERSISTED_KEY_CACHE_TTL:
        try:
            _persisted_key_cache = secret_store.has_persisted_key()
        except Exception:
            _persisted_key_cache = False
        _persisted_key_cache_ts = now
    return {
        "enabled": enabled,
        "effective": effective,
        "env_kill_switch": env_kill,
        "has_private_key": has_key,
        "persisted_in_keychain": _persisted_key_cache,
        "reason_blocked": reason,
    }


@app.get("/api/live/mode")
async def live_mode_get():
    """קורא מצב "כסף אמיתי" מהמנוע (נשלט מהממשק)."""
    return _live_mode_state()


class LiveModeBody(BaseModel):
    enabled: bool


@app.post("/api/live/mode")
async def live_mode_set(body: LiveModeBody):
    """מחליף מצב "כסף אמיתי" מהממשק — זה התחליף היחיד לעריכת .env.
    הערה: POLYMARKET_LIVE env נשאר kill-switch בלבד בפריסה ולא משמש כדי להפעיל לייב.
    """
    runner.rt.live_trading = bool(body.enabled)
    runner.rt.log(
        "מצב כסף אמיתי הופעל (מהממשק)" if runner.rt.live_trading else "מצב כסף אמיתי כובה (מהממשק)"
    )
    try:
        _save_persisted_config()
    except Exception:
        pass
    return {"ok": True, **_live_mode_state()}


@app.get("/api/live/polymarket-clob-account")
async def polymarket_clob_account():
    """יתרת collateral (USDC) ב-CLOB לפי המפתח הנוכחי — לא כל תיק Polymarket באתר."""
    return fetch_polymarket_clob_account()


@app.get("/api/live/portfolio")
async def live_portfolio(force: bool = False):
    """snapshot חי: יתרת USDC אמיתית + פוזיציות פתוחות של Polymarket + שווי נטו.
    זה מה שה-UI מציג במצב לייב במקום הספר הצללי של הסימולציה.
    """
    return await fetch_live_portfolio(force=force)


@app.post("/api/live/order")
async def live_order(body: TradeBody):
    """כסף חי — BUY דרך live_clob (tick size + neg_risk). צד Up/Down ב-body הוא שוק בלבד."""
    ok_sz, n_adj, verr = validate_contracts_for_market(
        float(body.contracts), float(body.order_min_size), bump_if_needed=True
    )
    if not ok_sz:
        raise HTTPException(400, verr or "גודל לא תקין")
    price = float(body.limit_price or 0.5)
    cfg = runner.rt.config
    return await live_place_entry_order(
        str(body.token_id),
        float(n_adj),
        price,
        "BUY",
        order_mode=getattr(cfg, "order_mode", "limit"),
        entry_slippage_pct=float(getattr(cfg, "entry_slippage_pct", 2.0)),
    )


@app.get("/api/signals")
async def api_signals(refresh: bool = False, window: Optional[str] = None):
    """
    מחזיר סיגנלים מאוחדים: TA + CLOB Imbalance + היסטוריה + סנטימנט.
    כולל המלצת כיוון (Up/Down/neutral) + אחוז ביטחון.
    פרמטר window: 5m | 15m (ברירת מחדל: לפי הגדרת btc_window של runner)
    """
    btc_win = window if window in ("5m", "15m") else runner.rt.config.btc_window
    m = await discover_active_btc_window(btc_win)
    up_book: Optional[dict[str, Any]] = None
    down_book: Optional[dict[str, Any]] = None
    if m:
        try:
            client = _get_shared_httpx()
            up_book = await get_clob_book(client, m.token_up)
            down_book = await get_clob_book(client, m.token_down)
        except Exception:
            pass
    window_sec = m.window_sec if m else 300
    result = await compute_signals(
        up_book=up_book,
        down_book=down_book,
        window_sec=window_sec,
        force_refresh=bool(refresh),
    )

    # מחירי Ask הנוכחיים לכל חוזה (¢) — ישירות מה-CLOB
    def _best_ask_cents(book: Optional[dict[str, Any]]) -> Optional[float]:
        if not book:
            return None
        asks = book.get("asks") or []
        if not asks:
            return None
        try:
            return round(float(asks[0]["price"]) * 100, 1)
        except Exception:
            return None

    result["contract_asks"] = {
        "up": _best_ask_cents(up_book),
        "down": _best_ask_cents(down_book),
    }
    result["market_slug"] = m.slug if m else None
    result["btc_window"] = btc_win

    return JSONResponse(content=result)


# ── Fast contract price endpoint ───────────────────────────────────────────────
# cache קצר — נפרד לכל window (5m / 15m)
_contract_price_cache: dict[str, dict[str, Any]] = {}
_contract_price_cache_ts: dict[str, float] = {}
CONTRACT_PRICE_CACHE_TTL = 0.5  # שניות


@app.get("/api/contract-prices")
async def api_contract_prices(window: Optional[str] = None):
    """
    מחזיר את מחירי ה-Ask הנוכחיים של חוזי Up/Down.
    קודם כל מנסה מ-WebSocket cache (אפס latency), אחרת CLOB REST.
    """
    btc_win = window if window in ("5m", "15m") else runner.rt.config.btc_window
    now = time.time()

    m = await discover_active_btc_window(btc_win)
    if m is None:
        return JSONResponse(content={"up": None, "down": None, "slug": None, "btc_window": btc_win, "ts": now})

    up_tp = price_stream.get_price(m.token_up)
    down_tp = price_stream.get_price(m.token_down)
    ws_fresh = (
        up_tp is not None and up_tp.ask is not None and (now - up_tp.ts) < 15.0
        and down_tp is not None and down_tp.ask is not None and (now - down_tp.ts) < 15.0
    )
    if ws_fresh:
        result = {
            "up": round(up_tp.ask * 100, 1) if up_tp and up_tp.ask else None,
            "down": round(down_tp.ask * 100, 1) if down_tp and down_tp.ask else None,
            "slug": m.slug,
            "btc_window": btc_win,
            "ts": now,
            "source": "ws",
        }
        return JSONResponse(content=result)

    cached = _contract_price_cache.get(btc_win)
    if cached and (now - _contract_price_cache_ts.get(btc_win, 0)) < CONTRACT_PRICE_CACHE_TTL:
        return JSONResponse(content=cached)

    up_ask: Optional[float] = None
    down_ask: Optional[float] = None
    try:
        client = _get_shared_httpx()
        up_book, down_book = await asyncio.gather(
            get_clob_book(client, m.token_up),
            get_clob_book(client, m.token_down),
            return_exceptions=True,
        )
        def _first_ask(book: Any) -> Optional[float]:
            if isinstance(book, Exception) or not isinstance(book, dict):
                return None
            asks = book.get("asks") or []
            if not asks:
                return None
            try:
                return round(float(asks[0]["price"]) * 100, 1)
            except Exception:
                return None
        up_ask = _first_ask(up_book)
        down_ask = _first_ask(down_book)
    except Exception:
        pass

    result = {"up": up_ask, "down": down_ask, "slug": m.slug, "btc_window": btc_win, "ts": now}
    _contract_price_cache[btc_win] = result
    _contract_price_cache_ts[btc_win] = now
    return JSONResponse(content=result)


class WindowResultBody(BaseModel):
    epoch: int
    slug: str
    side_won: Optional[str] = None
    btc_open: Optional[float] = None
    btc_close: Optional[float] = None
    window_sec: int = 300


@app.post("/api/history/record")
async def history_record(body: WindowResultBody):
    """שמירת תוצאת חלון לבסיס הנתונים ההיסטורי."""
    saved = record_window_result(
        epoch=body.epoch,
        slug=body.slug,
        side_won=body.side_won,
        btc_open=body.btc_open,
        btc_close=body.btc_close,
        window_sec=body.window_sec,
    )
    return {"ok": True, "saved": saved}


@app.get("/api/history/recent")
async def history_recent(limit: int = 20, window_sec: int = 300):
    """חלונות אחרונים מההיסטוריה."""
    rows = get_recent_windows(limit=min(int(limit), 100), window_sec=int(window_sec))
    return {"windows": rows}


@app.get("/api/history/hourly")
async def history_hourly(window_sec: int = 300):
    """פירוט win rate לפי שעה (0-23 UTC)."""
    rows = get_hourly_breakdown(window_sec=int(window_sec))
    return {"hourly": rows}


@app.get("/api/history/last-window-outcome")
async def history_last_window_outcome():
    """תוצאת חלון/ות אחרונים + תצוגה מקדימה של בחירת FLW (לתצוגת UI).

    מחזיר:
    - last: החלון האחרון שנסגר (epoch, side_won, btc_open/close, drift_pct).
    - flw_preview: לאיזה צד FLW היה נכנס עכשיו לפי הקונפיג הנוכחי. None אם
      FLW כבוי או שאין מספיק history.
    """
    c = runner.rt.config
    try:
        from market_discovery import window_step_sec
        ws = window_step_sec(getattr(c, "btc_window", "5m"))
    except Exception:
        ws = 300
    # תמיד לוקחים את החלון האחרון (ללא סינון drift) לתצוגה
    latest = get_last_window_winners(window_sec=ws, limit=1, min_drift_pct=0.0)
    last_out: Optional[dict[str, Any]] = None
    if latest:
        r = latest[0]
        bo = r.get("btc_open")
        bc = r.get("btc_close")
        drift_pct: Optional[float] = None
        if bo is not None and bc is not None:
            try:
                bof = float(bo)
                bcf = float(bc)
                if bof > 0:
                    drift_pct = round((bcf - bof) / bof * 100.0, 4)
            except (TypeError, ValueError):
                pass
        last_out = {
            "epoch": r.get("epoch"),
            "slug": r.get("slug"),
            "side_won": r.get("side_won"),
            "btc_open": bo,
            "btc_close": bc,
            "drift_pct": drift_pct,
            "window_sec": ws,
            "ts_recorded": r.get("ts_recorded"),
        }
    # תצוגה מקדימה של FLW לפי הקונפיג הנוכחי
    flw_preview: Optional[dict[str, Any]] = None
    if getattr(c, "follow_last_winner_enabled", False):
        try:
            side = runner._resolve_follow_winner_side(c)
            lookback = int(getattr(c, "follow_last_winner_lookback", 1) or 1)
            min_drift = float(getattr(c, "follow_last_winner_min_btc_drift_pct", 0.0) or 0.0)
            sample = get_last_window_winners(window_sec=ws, limit=lookback, min_drift_pct=min_drift)
            flw_preview = {
                "side": side,  # None אם אין history → fallback ל-side_preference
                "lookback": lookback,
                "mode": str(getattr(c, "follow_last_winner_mode", "forward")),
                "min_drift_pct": min_drift,
                "fallback_side_preference": getattr(c, "side_preference", "Up"),
                "samples": [
                    {
                        "epoch": s.get("epoch"),
                        "side_won": s.get("side_won"),
                        "btc_open": s.get("btc_open"),
                        "btc_close": s.get("btc_close"),
                    }
                    for s in sample
                ],
            }
        except Exception:
            flw_preview = None
    return {"last": last_out, "flw_preview": flw_preview, "window_sec": ws}


# ── Trigger API ────────────────────────────────────────────────────────────────

class TriggerConfigBody(BaseModel):
    mode: str = "off"
    momentum_pct: float = 0.20
    momentum_window_sec: int = 60
    momentum_direction: str = "auto"
    signal_confidence: float = 0.68
    signal_direction: str = "auto"
    dca_pulse_slices: int = 3
    dca_pulse_interval_sec: float = 20.0
    dca_pulse_direction: str = "Up"
    investment_usd: float = 5.0
    entry_price_cents: float = 30.0
    take_profit_pct: float = 15.0
    max_triggers_per_window: int = 2
    cooldown_sec: float = 60.0
    min_seconds_remaining: int = 90
    contract_max_drift_pct: float = 30.0
    auto_start: bool = False
    btc_window: str = "5m"
    dca_sizing: str = "equal"
    dca_min_step_pct: float = 0.0


@app.get("/api/trigger/state")
async def trigger_state():
    """מצב מנוע הטריגרים הנוכחי."""
    return JSONResponse(content=trigger.to_dict())


@app.post("/api/trigger/config")
async def trigger_config(body: TriggerConfigBody):
    """עדכון הגדרות הטריגר."""
    if body.mode not in ("off", "momentum", "signal", "dca_pulse"):
        raise HTTPException(400, "mode לא תקין")
    if body.momentum_direction not in ("auto", "Up", "Down"):
        raise HTTPException(400, "momentum_direction לא תקין")
    if body.signal_direction not in ("auto", "Up", "Down"):
        raise HTTPException(400, "signal_direction לא תקין")
    if body.dca_pulse_direction not in ("Up", "Down", "auto"):
        raise HTTPException(400, "dca_pulse_direction לא תקין")
    if body.btc_window not in ("5m", "15m"):
        raise HTTPException(400, "btc_window חייב להיות 5m או 15m")
    if body.dca_sizing not in ("equal", "pyramid", "fixed_contracts"):
        raise HTTPException(400, "dca_sizing לא תקין")
    c = trigger.config
    for k, v in body.model_dump().items():
        if hasattr(c, k):
            setattr(c, k, v)
    _save_trigger_config()
    return {"ok": True}


@app.post("/api/trigger/activate")
async def trigger_activate():
    """הפעלת הטריגר."""
    if trigger.config.mode == "off":
        raise HTTPException(400, "בחר מצב טריגר לפני הפעלה")
    from dataclasses import asdict
    trigger.config.active = True
    trigger._dca_running = False
    # חשוב: כדי ש-dca_pulse יוכל להתחיל מחדש גם אם המשתמש הפעיל מחדש
    # בתוך אותו epoch של חלון BTC (אחרת _dca_completed_epoch נועל את ההפעלה).
    trigger._dca_completed_epoch = -1
    trigger.status = "מופעל"
    # מבטיח שדף השידור יציג נתונים גם כשמפעילים רק "מסחר מהיר" (בלי semi/auto).
    if _bot_run_started_ts is None:
        _start_bot_run_session()
    append_event("trigger_activated", {
        "mode": trigger.config.mode,
        "btc_window": trigger.config.btc_window,
        "investment_usd": trigger.config.investment_usd,
        "entry_price_cents": trigger.config.entry_price_cents,
        "take_profit_pct": trigger.config.take_profit_pct,
        "dca_sizing": getattr(trigger.config, "dca_sizing", "equal"),
        "config": asdict(trigger.config),
    })
    return {"ok": True}


@app.post("/api/trigger/rearm")
async def trigger_rearm():
    """
    מאפשר "להריץ שוב" את DCA Pulse בתוך אותו חלון.
    בפועל: משחרר את נעילת epoch + מאפס cooldown כדי שהטיק הבא יוכל להתחיל.
    """
    trigger._dca_running = False
    trigger._dca_completed_epoch = -1
    trigger.last_trigger_ts = 0.0
    trigger.status = "🔄 הוכן להרצה מחדש"
    append_event("trigger_rearmed", {"mode": trigger.config.mode})
    return {"ok": True}


@app.post("/api/trigger/deactivate")
async def trigger_deactivate():
    """כיבוי הטריגר."""
    trigger.config.active = False
    trigger._dca_running = False
    trigger.status = "כבוי"
    # אם גם semi/auto כבוי — סוגרים את סשן השידור.
    if runner.rt.mode == "off":
        _clear_bot_run_session()
    append_event("trigger_deactivated", {
        "mode": trigger.config.mode,
        "triggers_this_window": trigger.triggers_this_window,
    })
    return {"ok": True}


@app.delete("/api/trigger/events")
async def trigger_clear_events():
    """ניקוי לוג האירועים."""
    trigger.events.clear()
    return {"ok": True}


def _tail_text_lines(path: Path, max_lines: int = 120, max_bytes: int = 120_000) -> list[str]:
    """קורא סוף קובץ בלי לקרוא את כולו (בערך) — שימושי ל-`combined.log`."""
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(size - max_bytes, 0)
            f.seek(start)
            chunk = f.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        return lines[-max_lines:]
    except Exception:
        return []


@app.get("/api/trigger/share-bundle", response_class=JSONResponse)
async def trigger_share_bundle():
    """
    מחזיר "חבילת שיתוף" טקסטית שמרכזת:
    ההגדרות האחרונות של "מסחר מהיר" + הסבר לפי הקוד + אירועי טריגר אחרונים + tail של לוגים.
    """
    base = log_run_dir()
    if not base:
        raise HTTPException(404, "אין LOG_RUN_DIR פעיל")

    events_path = base / "events.jsonl"
    combined_path = base / "combined.log"
    trades_path = base / "trades_summary.txt"
    diag_path = base / "run_diagnostics.txt"
    meta_path = base / "meta.json"

    latest_activated: Optional[dict[str, Any]] = None
    last_trigger_events: deque[dict[str, Any]] = deque(maxlen=16)

    if events_path.exists():
        try:
            with open(events_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    ev = obj.get("event")
                    if ev == "trigger_activated":
                        latest_activated = obj
                    if isinstance(ev, str) and ev.startswith("trigger_"):
                        last_trigger_events.append(obj)
        except Exception:
            pass

    # config (נוכחי) + config (מהלוג)
    from dataclasses import asdict

    current_cfg = asdict(trigger.config)
    cfg_from_log = (latest_activated.get("config") if latest_activated else None) or current_cfg

    # ── Live trigger + market snapshot (כדי להבין "ממתין לתנאים") ──────────────
    live_state = trigger.to_dict()
    market_snap: dict[str, Any] = {}
    asks_snap: dict[str, Any] = {"up": None, "down": None}
    seconds_left_snap: Optional[int] = None
    try:
        m_live = await discover_active_btc_window(str(cfg_from_log.get("btc_window") or trigger.config.btc_window))
        if m_live:
            seconds_left_snap = int(seconds_until_window_end(m_live.epoch, m_live.window_sec))
            market_snap = {
                "slug": m_live.slug,
                "epoch": m_live.epoch,
                "window_sec": m_live.window_sec,
                "seconds_left": seconds_left_snap,
                "btc_window": str(cfg_from_log.get("btc_window") or trigger.config.btc_window),
                "order_min_size": m_live.order_min_size,
            }
            # best asks לשני הצדדים
            client = _get_shared_httpx()
            up_book, down_book = await asyncio.gather(
                get_clob_book(client, m_live.token_up),
                get_clob_book(client, m_live.token_down),
            )
            asks_snap["up"] = float(up_book["asks"][0]["price"]) if (up_book.get("asks") or []) else None
            asks_snap["down"] = float(down_book["asks"][0]["price"]) if (down_book.get("asks") or []) else None
    except Exception as e:
        market_snap = {"error": repr(e)}

    # מידע תצוגה
    mode = str(cfg_from_log.get("mode") or trigger.config.mode)
    signal_threshold = float(cfg_from_log.get("signal_confidence") or current_cfg.get("signal_confidence") or 0.0)
    cooldown_sec = float(cfg_from_log.get("cooldown_sec") or current_cfg.get("cooldown_sec") or 0.0)
    min_seconds_remaining = float(
        cfg_from_log.get("min_seconds_remaining") or current_cfg.get("min_seconds_remaining") or 0.0
    )

    run_name = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            run_name = meta.get("run_name")
        except Exception:
            pass

    # אירועים אחרונים (פורמט קריא)
    from datetime import datetime

    def _fmt_ev_line(obj: dict[str, Any], idx: int) -> str:
        ts = obj.get("ts")
        ev = obj.get("event", "")
        side = obj.get("side")
        cap_price = obj.get("cap_price")
        ask = obj.get("contract_ask")
        contracts = obj.get("contracts")
        note = obj.get("note")
        time_str = ""
        if ts:
            try:
                time_str = datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
            except Exception:
                time_str = ""
        return (
            f"{idx}. [{time_str}] {ev} | side={side or '—'} | cap={cap_price if cap_price is not None else '—'} "
            f"| ask={ask if ask is not None else '—'} | contracts={contracts if contracts is not None else '—'}\n"
            f"   note: {note or '—'}"
        )

    ev_lines: list[str] = []
    for i, obj in enumerate(list(last_trigger_events)[-12:], start=1):
        ev_lines.append(_fmt_ev_line(obj, i))

    how_it_works: list[str] = []
    how_it_works.append(f"מצב טריגר: `{mode}`")
    how_it_works.append(f"`cooldown_sec`: {cooldown_sec} — בין טריגר לטריגר")
    how_it_works.append(f"`min_seconds_remaining`: {min_seconds_remaining} — לא נכנסים כשנשאר פחות")
    how_it_works.append(f"`entry_price_cents` (cap): {cfg_from_log.get('entry_price_cents')}¢ — לא משלמים מעל cap")
    how_it_works.append(f"`take_profit_pct`: {cfg_from_log.get('take_profit_pct')}% — TP אחרי כניסה")

    if mode == "signal":
        how_it_works.append(
            "במצב signal: המנוע קורא `compute_signals` ומקבל `recommendation`=Up/Down/neutral + confidence."
        )
        how_it_works.append(
            f"נכנסים רק אם `recommendation != neutral` וגם `confidence >= signal_confidence` "
            f"(אצלך {signal_threshold:.2f}) | signal_engine CONFIDENCE_THRESHOLD ברירת מחדל {CONFIDENCE_THRESHOLD:.2f}."
        )
    elif mode == "dca_pulse":
        how_it_works.append(
            "במצב dca_pulse: יש `dca_pulse_slices` סלייסים; כל סלייס מחכה ש-ask יהיה מתחת ל-cap."
        )
        how_it_works.append(
            "ב־dca_pulse_direction=auto: המנוע בוחר Up/Down לפי מי שיש מחיר ask מתאים מתחת ל-cap. "
            "אם recommendation יוצא neutral — עדיין לא אמור לפתוח `side=neutral`."
        )
    elif mode == "momentum":
        how_it_works.append(
            "במצב momentum: הבוט מחכה ל-BTC זז ב-% `momentum_pct` תוך `momentum_window_sec`, ואז נכנס Up/Down לפי `momentum_direction`."
        )

    tail_combined = _tail_text_lines(combined_path, max_lines=120)
    tail_trades = _tail_text_lines(trades_path, max_lines=60, max_bytes=80_000)
    tail_diag = _tail_text_lines(diag_path, max_lines=80, max_bytes=120_000)

    activated_ts = (latest_activated.get("ts") if latest_activated else None) if latest_activated else None
    activated_ts_line = ""
    if activated_ts:
        try:
            activated_ts_line = datetime.fromtimestamp(float(activated_ts)).isoformat()
        except Exception:
            activated_ts_line = str(activated_ts)

    bundle_lines: list[str] = []
    bundle_lines.append("# חבילת שיתוף — מסחר מהיר (TriggerEngine)")
    if run_name:
        bundle_lines.append(f"- run_name: {run_name}")
    bundle_lines.append(f"- log_run_dir: `{base}`")
    if latest_activated and activated_ts_line:
        bundle_lines.append(f"- trigger_activated ts: {activated_ts_line}")

    bundle_lines.append("\n## ההגדרות שהיו בשימוש")
    bundle_lines.append("```json")
    bundle_lines.append(json.dumps(cfg_from_log, ensure_ascii=False, indent=2))
    bundle_lines.append("```")

    bundle_lines.append("\n## מצב חי עכשיו (debug)")
    bundle_lines.append("```json")
    bundle_lines.append(json.dumps({
        "trigger_state": {
            "active": live_state.get("active"),
            "mode": live_state.get("mode"),
            "status": live_state.get("status"),
            "current_window_epoch": live_state.get("current_window_epoch"),
            "dca_running": live_state.get("dca_running"),
            "dca_completed_epoch": live_state.get("dca_completed_epoch"),
            "cooldown_remaining": live_state.get("cooldown_remaining"),
            "open_positions": live_state.get("open_positions"),
            "status_log_tail": (live_state.get("status_log") or [])[-12:],
            "events_tail": (live_state.get("events") or [])[-8:],
        },
        "market": market_snap,
        "best_asks": asks_snap,
    }, ensure_ascii=False, indent=2))
    bundle_lines.append("```")

    bundle_lines.append("\n## איך זה עובד (לפי הקוד)")
    bundle_lines.extend([f"- {x}" for x in how_it_works])

    if ev_lines:
        bundle_lines.append("\n## אירועי טריגר אחרונים")
        bundle_lines.append("```")
        bundle_lines.extend(ev_lines)
        bundle_lines.append("```")

    if tail_trades:
        bundle_lines.append("\n## Trades summary (tail)")
        bundle_lines.append("```")
        bundle_lines.extend(tail_trades)
        bundle_lines.append("```")

    if tail_diag:
        bundle_lines.append("\n## Run diagnostics (tail)")
        bundle_lines.append("```")
        bundle_lines.extend(tail_diag)
        bundle_lines.append("```")

    if tail_combined:
        bundle_lines.append("\n## combined.log (tail)")
        bundle_lines.append("```")
        bundle_lines.extend(tail_combined)
        bundle_lines.append("```")

    return {"ok": True, "text": "\n".join(bundle_lines)}


@app.get("/api/ws-prices/status")
async def ws_prices_status():
    """Status of the WebSocket price stream from Polymarket."""
    return {
        "connected": price_stream.connected,
        "last_msg_ts": price_stream.last_message_ts,
        "subscribed_tokens": len(price_stream._subscribed_tokens),
    }


@app.get("/api/prices/realtime")
async def prices_realtime():
    """Zero-latency price snapshot from WebSocket cache (no CLOB HTTP call)."""
    m = await discover_active_btc_window(runner.rt.config.btc_window)
    if not m:
        return JSONResponse(content={"up": None, "down": None, "ws_connected": price_stream.connected})
    up_tp = price_stream.get_price(m.token_up)
    down_tp = price_stream.get_price(m.token_down)
    now = time.time()
    return JSONResponse(content={
        "slug": m.slug,
        "up": {
            "bid": up_tp.bid if up_tp else None,
            "ask": up_tp.ask if up_tp else None,
            "mid": up_tp.mid if up_tp else None,
            "age_ms": int((now - up_tp.ts) * 1000) if up_tp and up_tp.ts else None,
        },
        "down": {
            "bid": down_tp.bid if down_tp else None,
            "ask": down_tp.ask if down_tp else None,
            "mid": down_tp.mid if down_tp else None,
            "age_ms": int((now - down_tp.ts) * 1000) if down_tp and down_tp.ts else None,
        },
        "ws_connected": price_stream.connected,
        "ts": now,
    })


@app.websocket("/ws/prices")
async def ws_prices_endpoint(websocket: WebSocket):
    """WebSocket endpoint: streams real-time price updates to the frontend."""
    await websocket.accept()
    rid = make_request_id()
    ws_start = time.time()
    client_ip = websocket.client.host if websocket.client else None
    msgs_sent = 0
    log_request_event(
        source="server",
        kind="ws_open",
        request_id=rid,
        method="WS",
        path="/ws/prices",
        client_ip=client_ip,
    )
    client = price_stream.register_frontend_client()
    try:
        # Send initial snapshot
        for tid in list(price_stream._subscribed_tokens):
            tp = price_stream.get_price(tid)
            if tp and (tp.bid is not None or tp.ask is not None):
                side_label = price_stream._token_to_side.get(tid, "unknown")
                await websocket.send_json({
                    "type": "price",
                    "token_id": tid,
                    "side": side_label,
                    "bid": tp.bid,
                    "ask": tp.ask,
                    "mid": tp.mid,
                    "ts": tp.ts,
                })
                msgs_sent += 1
        while True:
            msgs = await client.drain_with_timeout(15.0)
            if not msgs:
                try:
                    await websocket.send_json({"type": "ping"})
                    msgs_sent += 1
                except Exception:
                    break
                continue
            for msg in msgs:
                await websocket.send_text(msg)
                msgs_sent += 1
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        price_stream.unregister_frontend_client(client)
        log_request_event(
            source="server",
            kind="ws_close",
            request_id=rid,
            method="WS",
            path="/ws/prices",
            duration_ms=round((time.time() - ws_start) * 1000.0, 1),
            messages_sent=msgs_sent,
            client_ip=client_ip,
        )


@app.get("/remote", include_in_schema=False)
async def remote_control():
    html = """
<!doctype html>
<html lang="he" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>שליטה מהירה בבוט</title>
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0b1020;
      color: #eef2ff;
    }
    .wrap {
      max-width: 560px;
      margin: 24px auto;
      padding: 16px;
    }
    .card {
      background: #121a33;
      border: 1px solid #273056;
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 8px 20px rgba(0,0,0,0.28);
    }
    h1 { margin: 0 0 10px; font-size: 20px; }
    .row { display: flex; gap: 10px; margin-top: 10px; }
    button {
      flex: 1;
      border: 0;
      border-radius: 10px;
      padding: 12px;
      font-weight: 700;
      color: #fff;
      cursor: pointer;
    }
    .auto { background: #0b8f4d; }
    .semi { background: #2663d7; }
    .off { background: #a53434; }
    .ghost {
      margin-top: 12px;
      width: 100%;
      background: #263257;
    }
    .muted { color: #a9b5db; font-size: 13px; margin-top: 8px; }
    .status { margin-top: 12px; font-size: 15px; font-weight: 600; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>שליטה מהירה בבוט</h1>
      <div class="muted">דף קצר לנייד: מעבר מצב בלחיצה אחת.</div>
      <div class="status" id="state">טוען מצב...</div>
      <div class="row">
        <button class="auto" onclick="setMode('auto')">AUTO</button>
        <button class="semi" onclick="setMode('semi')">SEMI</button>
        <button class="off" onclick="setMode('off')">OFF</button>
      </div>
      <button class="ghost" onclick="refreshState()">רענון מצב</button>
      <div class="muted" id="last"></div>
    </div>
  </div>
<script>
  async function refreshState() {
    const stateEl = document.getElementById('state');
    const lastEl = document.getElementById('last');
    try {
      const r = await fetch('/api/strategy/config');
      const data = await r.json();
      const mode = (data && data.mode) ? String(data.mode).toUpperCase() : 'UNKNOWN';
      stateEl.textContent = 'מצב נוכחי: ' + mode;
      lastEl.textContent = 'עודכן: ' + new Date().toLocaleTimeString();
    } catch (e) {
      stateEl.textContent = 'שגיאה בקריאת מצב';
      lastEl.textContent = String(e);
    }
  }

  async function setMode(mode) {
    const stateEl = document.getElementById('state');
    const lastEl = document.getElementById('last');
    stateEl.textContent = 'מעדכן מצב ל-' + mode.toUpperCase() + '...';
    try {
      const r = await fetch('/api/strategy/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode })
      });
      if (!r.ok) {
        const t = await r.text();
        throw new Error(t || 'request failed');
      }
      await refreshState();
    } catch (e) {
      stateEl.textContent = 'עדכון מצב נכשל';
      lastEl.textContent = String(e);
    }
  }

  refreshState();
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.get("/", include_in_schema=False)
async def web_root():
    index_path = WEB_DIST_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    raise HTTPException(404, "Web UI not built (missing dist/index.html)")


@app.get("/{full_path:path}", include_in_schema=False)
async def web_spa(full_path: str):
    # Keep API routes untouched
    if full_path.startswith("api/"):
        raise HTTPException(404, "Not Found")

    if not WEB_DIST_DIR.exists():
        raise HTTPException(404, "Web UI not built")

    wanted = (WEB_DIST_DIR / full_path).resolve()
    try:
        wanted.relative_to(WEB_DIST_DIR)
    except Exception:
        raise HTTPException(404, "Not Found")

    if wanted.is_file():
        return FileResponse(str(wanted))

    index_path = WEB_DIST_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    raise HTTPException(404, "Web UI not built (missing dist/index.html)")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8767)
