"""
כתיבת לוגים מובנים לתיקיית ריצה כאשר מוגדר משתנה סביבה LOG_RUN_DIR.
נועד לשליחה לניתוח / דיבוג — כולל snapshot של אסטרטגיה ומצב דמו.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Optional

from demo_engine import DemoEngine
from strategy_runner import StrategyRunner

# תואם ל-demo_engine.FEE_RATE — עמלת מימוש בלוגים
_FEE = 0.002


def format_duration_sec(sec: float | None) -> str:
    """פורמט קריא למשך מחזור (שניות צף)."""
    if sec is None or not math.isfinite(sec) or sec < 0:
        return "—"
    s = int(sec % 60)
    m = int((sec // 60) % 60)
    h = int(sec // 3600)
    if h > 0:
        return f"{h} שע׳ {m} דק׳"
    if m > 0:
        return f"{m} דק׳ {s} שנ׳"
    return f"{s} שנ׳"


def session_times_from_trades(sess_trades: list[dict[str, Any]]) -> tuple[float, float | None, float | None]:
    """
    זמן כניסה ראשונה (מינימום ts של BUY), זמן יציאה אחרון, משך בשניות.
    אם אין יציאה — end ו-duration הם None.
    """
    buys = [t for t in sess_trades if t.get("type") == "BUY"]
    exits = [
        t
        for t in sess_trades
        if t.get("type")
        in ("SELL_TP", "EXPIRE_0", "SETTLE_WIN", "SETTLE_LOSS", "SETTLE_UNKNOWN")
        or (t.get("type") and "SELL" in str(t.get("type", "")))
    ]

    def _ts_list(arr: list[dict[str, Any]]) -> list[float]:
        out = [float(x.get("ts") or 0) for x in arr]
        return [x for x in out if x > 0]

    buy_ts = _ts_list(buys)
    all_ts = _ts_list(sess_trades)
    exit_ts = _ts_list(exits)
    start = min(buy_ts) if buy_ts else (min(all_ts) if all_ts else 0.0)
    end = max(exit_ts) if exit_ts else None
    if start <= 0:
        return 0.0, end, None
    duration = (end - start) if end is not None and end >= start else None
    return start, end, duration


def _leg_cost_from_tp_exit_dict(trade: dict[str, Any]) -> float | None:
    c = float(trade.get("contracts") or 0)
    px = float(trade.get("price") or 0)
    rp = trade.get("realized_pnl")
    if c <= 0 or px <= 0 or rp is None:
        return None
    proceeds = px * c * (1 - _FEE)
    leg = proceeds - float(rp)
    return leg if leg > 0 else None


def _bid_from_potential_pct(leg_cost: float, contracts: float, pct: float) -> float | None:
    if contracts <= 0 or leg_cost <= 0:
        return None
    leg_val = leg_cost * (1 + pct / 100.0)
    return leg_val / (contracts * (1 - _FEE))


def format_potential_after_tp_log_lines(exit_trade: dict[str, Any]) -> list[str]:
    """
    שורות טקסט להיפותטי אחרי TP: מול יציאה (¢, Δ, % מול יציאה) + מול עלות (%).
    """
    pp = exit_trade.get("potential_peak_unrealized_pct")
    pt = exit_trade.get("potential_trough_unrealized_pct")
    if pp is None and pt is None:
        return []
    price = float(exit_trade.get("price") or 0)
    c = float(exit_trade.get("contracts") or 0)
    if c <= 0 or price <= 0:
        return []
    leg_cost = _leg_cost_from_tp_exit_dict(exit_trade)
    if leg_cost is None:
        return []
    lines: list[str] = []
    exit_c = price * 100.0
    lines.append(f"  מחיר יציאת TP: {exit_c:.1f}¢")
    if pp is not None:
        bid_p = _bid_from_potential_pct(leg_cost, c, float(pp))
        if bid_p is not None:
            d_c = (bid_p - price) * 100.0
            pve = ((bid_p - price) / price) * 100.0 if price else 0.0
            lines.append(
                f"  אחרי TP — שיא היפותטי: bid ~{bid_p * 100:.1f}¢ | "
                f"Δ {d_c:+.1f}¢ / {pve:+.1f}% מול יציאה | {pp:.1f}% מול עלות"
            )
        else:
            lines.append(f"  אחרי TP — שיא (מול עלות): {pp:.1f}%")
    if pt is not None:
        bid_t = _bid_from_potential_pct(leg_cost, c, float(pt))
        if bid_t is not None:
            d_c = (bid_t - price) * 100.0
            pve = ((bid_t - price) / price) * 100.0 if price else 0.0
            lines.append(
                f"  אחרי TP — שפל היפותטי: bid ~{bid_t * 100:.1f}¢ | "
                f"Δ {d_c:+.1f}¢ / {pve:+.1f}% מול יציאה | {pt:.1f}% מול עלות"
            )
        else:
            lines.append(f"  אחרי TP — שפל (מול עלות): {pt:.1f}%")
    return lines


def log_potential_window_closed(exit_trade: dict[str, Any]) -> None:
    """נקרא כשנסגר מעקב bid אחרי TP (סוף חלון) — נרשם ליומן ול-events."""
    lines = format_potential_after_tp_log_lines(exit_trade)
    if not lines:
        return
    base = log_run_dir()
    sid = exit_trade.get("session_id") or ""
    block = "\n".join(lines)
    append_strategy_journal(
        f"\n--- היפותטי אחרי TP (סוף חלון) — session {sid} ---\n{block}\n"
    )
    if base:
        append_jsonl(
            base / "events.jsonl",
            {
                "ts": time.time(),
                "event": "potential_after_tp_final",
                "session_id": sid,
                "token_id": exit_trade.get("token_id"),
                "lines": lines,
            },
        )


def log_run_dir() -> Optional[Path]:
    p = os.environ.get("LOG_RUN_DIR", "").strip()
    if not p:
        return None
    return Path(p)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def strategy_config_dict(runner: StrategyRunner) -> dict[str, Any]:
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
    }


def runtime_state_dict(runner: StrategyRunner) -> dict[str, Any]:
    rt = runner.rt
    return {
        "mode": rt.mode,
        "current_epoch": rt.current_epoch,
        "last_status": rt.last_status,
        "last_tick_ts": rt.last_tick_ts,
        "dca_done_slices": rt.dca_done_slices,
        "last_dca_ts": rt.last_dca_ts,
        "dca_last_fill_price": rt.dca_last_fill_price,
        "tp_happened_this_window": rt.tp_happened_this_window,
        "last_tp_ts": rt.last_tp_ts,
        "last_tp_side": rt.last_tp_side,
        "entries_this_window": rt.entries_this_window,
        "notional_this_window": rt.notional_this_window,
        "hedge_leg2_done": rt.hedge_leg2_done,
        "pending_approval": rt.pending_approval,
        "strategy_log_lines_count": len(rt.log_lines),
    }


def demo_summary(demo: DemoEngine) -> dict[str, Any]:
    """נתונים לדיבוג וניתוח: יתרה, equity, רווחים/הפסדים, פוזיציות."""
    st = demo.state
    lm = st.last_mark or {}
    equity = lm.get("equity") or (st.balance_usd + sum(p.contracts * p.avg_cost for p in st.positions))
    unrealized = lm.get("unrealized_usd", 0.0)
    total_realized = sum(float(t.get("realized_pnl") or 0) for t in st.trades)
    recent = [
        {
            "ts": t.get("ts"),
            "type": t.get("type"),
            "side": t.get("side"),
            "session_id": t.get("session_id"),
            "realized_pnl": t.get("realized_pnl"),
        }
        for t in st.trades[-30:]
    ]
    legs = lm.get("legs") or []
    positions_summary = [
        {
            "side": leg.get("side"),
            "token_id": (leg.get("token_id") or "")[:12],
            "contracts": leg.get("contracts"),
            "leg_unrealized": leg.get("leg_unrealized"),
            "peak_unrealized_pct": leg.get("peak_unrealized_pct"),
            "trough_unrealized_pct": leg.get("trough_unrealized_pct"),
        }
        for leg in legs
    ]
    return {
        "balance_usd": st.balance_usd,
        "equity_usd": equity,
        "unrealized_usd": unrealized,
        "total_realized_pnl": round(total_realized, 2),
        "positions_count": len(st.positions),
        "trades_count": len(st.trades),
        "equity_history_points": len(st.equity_history),
        "recent_trades_pnl": recent,
        "positions_summary": positions_summary,
    }


def diagnostics_dict(runner: StrategyRunner) -> dict[str, Any]:
    """נתונים לזיהוי תקיעות, בעיות ובריאות המערכת."""
    rt = runner.rt
    now = time.time()
    sec_since_tick = now - rt.last_tick_ts if rt.last_tick_ts else None
    potentially_stuck = (
        rt.mode == "auto"
        and rt.last_tick_ts > 0
        and sec_since_tick is not None
        and sec_since_tick > 90
    )
    return {
        "seconds_since_last_tick": round(sec_since_tick, 1) if sec_since_tick is not None else None,
        "potentially_stuck": potentially_stuck,
        "last_tick_ts": rt.last_tick_ts,
    }


def full_snapshot(runner: StrategyRunner, demo: DemoEngine) -> dict[str, Any]:
    return {
        "ts": time.time(),
        "ts_iso_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "strategy_config": strategy_config_dict(runner),
        "runtime": runtime_state_dict(runner),
        "diagnostics": diagnostics_dict(runner),
        "demo": demo_summary(demo),
        "strategy_logs_tail": runner.rt.log_lines[-50:],
        "log_entries": runner.rt.log_entries[-500:],
    }


def _load_meta_run_name(base: Path) -> str:
    """טוען שם הריצה מ-meta.json אם קיים."""
    try:
        with open(base / "meta.json", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("run_name") or data.get("run_relative_path") or str(base.name)
    except Exception:
        return time.strftime("polymarket-bot %Y-%m-%d %H-%M-%S", time.localtime())


def write_run_info(runner: StrategyRunner, demo: DemoEngine) -> None:
    """כתיבת run_info.txt — שם הריצה, תאריך, אסטרטגיה (סקירה מהירה)."""
    base = log_run_dir()
    if not base:
        return
    run_name = _load_meta_run_name(base)
    c = runner.rt.config
    rt = runner.rt
    lines = [
        "=" * 60,
        "שם הריצה: " + run_name,
        "תאריך/שעה: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "תיקייה: " + str(base),
        "=" * 60,
        "",
        "אסטרטגיה:",
        f"  מצב: {rt.mode}",
        f"  השקעה: ${c.investment_usd} | כניסה: {c.entry_price_cents}¢ | TP: {c.take_profit_pct}%",
        f"  DCA: {'כן' if c.dca_enabled else 'לא'}",
    ]
    if c.dca_enabled:
        lines.append(f"    סלייסים: {c.dca_slices} | מרווח: {c.dca_interval_sec}s | override: {c.dca_tp_override_pct}%")
    lines.extend([
        f"  גידור: {'כן' if c.hedge_enabled else 'לא'} | צד: {c.side_preference}",
        f"  דקות כניסה מינ': {c.min_minutes_for_entry} | קפיאה: {c.freeze_last_minutes}",
        "",
    ])
    try:
        with open(base / "run_info.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def write_engine_startup(runner: StrategyRunner, demo: DemoEngine) -> None:
    base = log_run_dir()
    if not base:
        return
    write_run_info(runner, demo)
    payload = {
        "event": "engine_startup",
        **full_snapshot(runner, demo),
        "python": os.environ.get("PYTHON", ""),
        "log_run_dir": str(base),
    }
    _write_json(base / "engine_startup.json", payload)
    append_jsonl(base / "events.jsonl", {"ts": time.time(), "event": "engine_startup", "mode": runner.rt.mode})
    write_trades_log(demo)


def _group_trades_by_session(trades: list[dict]) -> list[tuple[str, list[dict]]]:
    """קיבוץ עסקאות לפי session_id — כמו groupTradesBySession בפרונט."""
    by_session: dict[str, list[dict]] = {}
    for t in trades:
        sid = t.get("session_id") or (t.get("id") if t.get("type") == "BUY" else None) or f"orphan-{t.get('id', '')}"
        by_session.setdefault(sid, []).append(t)
    for lst in by_session.values():
        lst.sort(key=lambda x: float(x.get("ts") or 0))
    return sorted(
        by_session.items(),
        key=lambda kv: (kv[1][0].get("ts") or 0) if kv[1] else 0,
        reverse=True,
    )


def write_trades_log(demo: DemoEngine) -> None:
    """כתיבת כל העסקאות והמחזורים ללוגים — trades.json + trades_summary.txt."""
    base = log_run_dir()
    if not base:
        return
    st = demo.state
    trades = st.trades
    # trades.json — כל העסקאות (לניתוח)
    data = {
        "ts": time.time(),
        "trades_count": len(trades),
        "trades": trades,
    }
    _write_json(base / "trades.json", data)
    # trades_summary.txt — סיכום קריא לפי מחזורים
    groups = _group_trades_by_session(trades)
    lines = [
        "=" * 72,
        "סיכום עסקאות לפי מחזור — " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "=" * 72,
        f"סה״כ מחזורים: {len(groups)} | סה״כ עסקאות: {len(trades)}",
        "שיא/שפל % — יחסית לעלות הרגל (כולל עמלת כניסה), לא מסך יתרת החשבון.",
        "",
    ]
    for idx, (sid, sess_trades) in enumerate(groups):
        buys = [t for t in sess_trades if t.get("type") == "BUY"]
        exit_types = (
            "SELL_TP",
            "EXPIRE_0",
            "SETTLE_WIN",
            "SETTLE_LOSS",
            "SETTLE_UNKNOWN",
        )
        exits = [
            t
            for t in sess_trades
            if t.get("type") in exit_types
            or (t.get("type") and "SELL" in str(t.get("type", "")))
        ]
        side = buys[0].get("side", "—") if buys else "—"
        last_et = exits[-1].get("type") if exits else ""
        if last_et == "SETTLE_WIN":
            exit_type = "SETTLE_WIN"
        elif last_et in ("EXPIRE_0", "SETTLE_LOSS", "SETTLE_UNKNOWN"):
            exit_type = "EXPIRE"
        elif last_et == "SELL_TP" or (last_et and str(last_et).startswith("SELL")):
            exit_type = "TP"
        else:
            exit_type = ""
        realized = sum(float(t.get("realized_pnl") or 0) for t in sess_trades)
        peak = exits[-1].get("peak_unrealized_pct") if exits else None
        trough = exits[-1].get("trough_unrealized_pct") if exits else None
        peak_s = f"{peak:.1f}%" if peak is not None else "—"
        trough_s = f"{trough:.1f}%" if trough is not None else "—"
        start_ts, end_ts, dur_sec = session_times_from_trades(sess_trades)
        t_start = (
            time.strftime("%H:%M:%S", time.localtime(start_ts)) if start_ts and start_ts > 0 else "—"
        )
        t_end = time.strftime("%H:%M:%S", time.localtime(end_ts)) if end_ts else "—"
        summary = f"עסקה #{idx + 1} — {side}"
        if len(buys) > 1:
            summary += f" DCA ×{len(buys)}"
        if exit_type:
            summary += f" → {exit_type}"
        if realized != 0:
            summary += f" {realized:+.2f}$"
        if dur_sec is not None:
            summary += f" | משך מחזור {format_duration_sec(dur_sec)} (כניסה {t_start} → יציאה {t_end})"
        if peak is not None or trough is not None:
            summary += f" | בזמן החזקה (מול עלות): שיא {peak_s} שפל {trough_s}"
        lines.append(f"{summary}  [session: {sid}]")
        last_exit = exits[-1] if exits else None
        if last_exit and last_exit.get("type") == "SELL_TP":
            pot_lines = format_potential_after_tp_log_lines(last_exit)
            for pl in pot_lines:
                lines.append(f"    {pl}")
    try:
        with open(base / "trades_summary.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def write_strategy_snapshot(runner: StrategyRunner, demo: DemoEngine) -> None:
    base = log_run_dir()
    if not base:
        return
    snap = full_snapshot(runner, demo)
    _write_json(base / "strategy_snapshot.json", snap)
    write_journal_by_session(snap.get("log_entries", []))
    write_run_diagnostics(snap)
    write_trades_log(demo)


def write_journal_by_session(log_entries: list[dict]) -> None:
    """כתיבת לוגים מסודרים לפי מחזור עסקה (session_id)."""
    base = log_run_dir()
    if not base:
        return
    # קיבוץ לפי session_id
    by_session: dict[str, list[dict]] = {}
    no_session: list[dict] = []
    for e in log_entries:
        sid = e.get("session_id")
        if sid:
            by_session.setdefault(sid, []).append(e)
        else:
            no_session.append(e)
    # JSON מובנה
    data = {
        "ts": time.time(),
        "sessions": {k: sorted(v, key=lambda x: x["ts"]) for k, v in by_session.items()},
        "no_session": sorted(no_session, key=lambda x: x["ts"]),
    }
    _write_json(base / "journal_by_session.json", data)
    # קובץ טקסט קריא — כל מחזור בבלוק נפרד
    txt_path = base / "journal_by_session.txt"
    lines = [
        "=" * 72,
        "יומן לפי מחזור עסקה — " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "=" * 72,
        "",
    ]
    for sid, entries in sorted(by_session.items(), key=lambda kv: min(e["ts"] for e in kv[1])):
        lines.append("-" * 60)
        lines.append(f"מחזור עסקה: {sid}")
        lines.append("-" * 60)
        for e in sorted(entries, key=lambda x: x["ts"]):
            ts_h = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            t = e.get("type", "")
            msg = e.get("msg", "")
            lines.append(f"  [{ts_h}] {t}: {msg}")
        lines.append("")
    if no_session:
        lines.append("-" * 60)
        lines.append("ללא מחזור (אירועים כלליים)")
        lines.append("-" * 60)
        for e in sorted(no_session, key=lambda x: x["ts"]):
            ts_h = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
            t = e.get("type", "")
            msg = e.get("msg", "")
            lines.append(f"  [{ts_h}] {t}: {msg}")
        lines.append("")
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def write_run_diagnostics(snap: dict[str, Any]) -> None:
    """קובץ טקסט קריא לזיהוי בעיות, תקיעות ורווחים/הפסדים."""
    base = log_run_dir()
    if not base:
        return
    diag = snap.get("diagnostics") or {}
    demo = snap.get("demo") or {}
    lines = [
        "=" * 60,
        "אבחון ריצה — " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "=" * 60,
        "",
        "--- סטטוס מערכת ---",
    ]
    sec = diag.get("seconds_since_last_tick")
    if sec is not None:
        lines.append(f"  שניות מאז tick אחרון: {sec:.1f}")
        if diag.get("potentially_stuck"):
            lines.append("  ⚠️  ייתכן תקיעה — אין tick למעל 90 שניות במצב אוטו")
    else:
        lines.append("  שניות מאז tick: לא התחיל")
    lines.extend([
        "",
        "--- רווחים והפסדים ---",
        f"  יתרה ($): {demo.get('balance_usd', 0):.2f}",
        f"  equity משוער ($): {demo.get('equity_usd', 0):.2f}",
        f"  רווח/הפסד לא ממומש ($): {demo.get('unrealized_usd', 0):.2f}",
        f"  סה״כ רווח ממומש מכל העסקאות ($): {demo.get('total_realized_pnl', 0):.2f}",
        f"  עסקאות סה״כ: {demo.get('trades_count', 0)}",
        "",
    ])
    pos = demo.get("positions_summary") or []
    if pos:
        lines.append("--- פוזיציות פתוחות ---")
        for p in pos:
            upnl = p.get("leg_unrealized")
            upnl_s = f"${upnl:.2f}" if upnl is not None else "—"
            peak = p.get("peak_unrealized_pct")
            trough = p.get("trough_unrealized_pct")
            peak_s = f"{peak:.1f}%" if peak is not None else "—"
            trough_s = f"{trough:.1f}%" if trough is not None else "—"
            lines.append(
                f"  {p.get('side', '?')} {p.get('contracts', 0):.0f} חוזים: upnl={upnl_s} | "
                f"שיא/שפל (מול עלות): {peak_s} / {trough_s}"
            )
        lines.append("")
    recent = demo.get("recent_trades_pnl") or []
    if recent:
        pnl_trades = [r for r in recent if r.get("realized_pnl") is not None]
        if pnl_trades:
            lines.append("--- עסקאות אחרונות (יציאות) ---")
            for t in pnl_trades[-10:]:
                pnl = t.get("realized_pnl", 0)
                lines.append(f"  {t.get('type', '')} {t.get('side', '')}: ${pnl:.2f}  (session: {t.get('session_id', '')})")
            lines.append("")
    try:
        with open(base / "run_diagnostics.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def log_error(msg: str, context: Optional[dict[str, Any]] = None) -> None:
    """רישום שגיאה ל־events.jsonl לניתוח בעיות."""
    base = log_run_dir()
    if not base:
        return
    append_jsonl(
        base / "events.jsonl",
        {"ts": time.time(), "event": "error", "msg": msg, **(context or {})},
    )


def _journal_header_lines(runner: StrategyRunner, demo: DemoEngine) -> list[str]:
    c = runner.rt.config
    rt = runner.rt
    st = demo.state
    h = [
        "=" * 72,
        "יומן אסטרטגיה — Polymarket Bot",
        "=" * 72,
        "",
        f"תאריך/שעה התחלה: {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime())}",
        f"תיקיית לוגים: {log_run_dir() or ''}",
        "",
        "--- אסטרטגיה פעילה (פרמטרים) ---",
        f"  מצב (mode): {rt.mode}",
        f"  השקעה ($): {c.investment_usd}",
        f"  מחיר כניסה (סנט): {c.entry_price_cents}",
        f"  מינימום חוזים: {c.min_contracts}",
        f"  שוק BTC: חלון {getattr(c, 'btc_window', '5m')} (5m / 15m — Up/Down)",
        f"  יעד רווח (TP %): {c.take_profit_pct}",
        f"  דקות מינימום לכניסה: {c.min_minutes_for_entry}",
        f"  דקות קפיאה אחרונות: {c.freeze_last_minutes}",
        f"  חסימת אזור ביניים: {c.intermediate_block_new_entries}",
        f"  DCA: {'כן' if c.dca_enabled else 'לא'}",
    ]
    if c.dca_enabled:
        h.extend([
            f"    סלייסים: {c.dca_slices}",
            f"    מרווח (שניות): {c.dca_interval_sec}",
            f"    הנחה בין כניסות: {'כן' if c.dca_discount_enabled else 'לא'} ({c.dca_discount_pct}%)",
        ])
    h.extend([
        f"  גידור: {'כן' if c.hedge_enabled else 'לא'}",
    ])
    if c.hedge_enabled:
        h.append(f"    סף Ask משולב מקס': {c.hedge_combined_ask_max}")
    h.extend([
        f"  העדפת צד: {c.side_preference}",
        f"  רה־כניסה אחרי TP: {'כן' if c.auto_reenter_after_tp else 'לא'}",
        f"  Cooldown אחרי TP (שניות): {c.reenter_cooldown_sec}",
        f"  מקס' כניסות בחלון: {c.max_entries_per_window}",
        f"  מקס' עסקאות לשעה: {c.max_trades_per_hour}",
        f"  תקרת חשיפה בחלון ($): {c.max_notional_per_window_usd}",
        f"  DCA override TP (%): {c.dca_tp_override_pct}",
        "",
        "--- מצב ריצה בעלייה ---",
        f"  epoch נוכחי: {rt.current_epoch}",
        f"  DCA סלייסים שבוצעו: {rt.dca_done_slices}",
        f"  עסקאות בחלון: {rt.entries_this_window}",
        "",
        "--- דמו (בעלייה) ---",
        f"  יתרה ($): {st.balance_usd:.2f}",
        f"  פוזיציות פתוחות: {len(st.positions)}",
        f"  סה״כ עסקאות: {len(st.trades)}",
        "",
        "--- יומן (בהמשך) ---",
        "",
    ])
    return h


def write_strategy_journal_header(runner: StrategyRunner, demo: DemoEngine) -> None:
    base = log_run_dir()
    if not base:
        return
    path = base / "strategy_journal.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    h = _journal_header_lines(runner, demo)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


def append_strategy_journal(line: str) -> None:
    """כתיבת שורת יומן לקובץ — נקרא מכל מקום שמדפיס ליומן."""
    base = log_run_dir()
    if not base:
        return
    path = base / "strategy_journal.txt"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass


def append_event(name: str, payload: dict[str, Any]) -> None:
    base = log_run_dir()
    if not base:
        return
    append_jsonl(
        base / "events.jsonl",
        {"ts": time.time(), "event": name, **payload},
    )


async def periodic_snapshot_loop(runner: StrategyRunner, demo: DemoEngine, interval_sec: float = 60.0) -> None:
    """רקע: snapshot מיידי ואז כל interval_sec — strategy_snapshot.json מעודכן."""
    base = log_run_dir()
    if not base:
        return
    try:
        write_strategy_snapshot(runner, demo)
    except Exception as e:
        append_event("snapshot_error", {"error": repr(e)})
    while True:
        await asyncio.sleep(interval_sec)
        try:
            write_strategy_snapshot(runner, demo)
        except Exception as e:
            append_event("snapshot_error", {"error": repr(e)})
