"""ה-watchdog חייב למדוד תקיעת-tick מול שעון monotonic, לא מול wall-clock.

רגרסיה מאומתת: בחלון שינה אחד של ה-Mac ה-wall-clock (time.time()) קופץ בזמן
שהלולאה האסינכרונית בריאה → `strategy_tick_stalled` נורתה 52× כ-false-positive
בעוד ה-`event_loop_lag` המונוטוני נורה 3× בלבד. בדיקת התקיעה חייבת להתבסס על
`last_tick_monotonic` (שנעצר בזמן שינת מערכת), לא על `last_tick_ts`.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import fault_tracker
import main


@pytest.fixture(autouse=True)
def _tmp_faults_db(tmp_path: Path, monkeypatch):
    """DB תקלות נקי לכל טסט (מראה את test_fault_tracker)."""
    monkeypatch.setattr(fault_tracker, "_DB_PATH", tmp_path / "faults.db")
    monkeypatch.setattr(fault_tracker, "_conn", None)
    yield


@pytest.fixture(autouse=True)
def _restore_rt():
    """שומר/משחזר את מצב ה-runtime הגלובלי כדי לא לזהם טסטים אחרים."""
    rt = main.runner.rt
    saved = (rt.mode, rt.last_tick_ts, getattr(rt, "last_tick_monotonic", 0.0))
    yield
    rt.mode, rt.last_tick_ts, rt.last_tick_monotonic = saved


def _run_one_watchdog_pass() -> None:
    """מריץ כמה איטרציות זעירות של ה-watchdog ואז מבטל."""

    async def _drive():
        task = asyncio.create_task(main._loop_watchdog(interval_sec=0.01))
        await asyncio.sleep(0.08)  # מספיק למספר מעברים על בדיקת התקיעה
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())


def _stall_faults() -> list:
    return [
        f for f in fault_tracker.list_faults()
        if f["dedup_key"] == "strategy_tick_stalled"
    ]


def test_wall_clock_jump_does_not_false_positive():
    """מצב שינת-Mac: wall-clock ישן (545s) אך heartbeat מונוטוני טרי → אין תקלה."""
    rt = main.runner.rt
    rt.mode = "auto"
    rt.last_tick_ts = time.time() - 545     # ה-wall-clock קפץ (המערכת ישנה)
    rt.last_tick_monotonic = time.monotonic()  # אך הלולאה תיקתקה ממש עכשיו
    _run_one_watchdog_pass()
    assert _stall_faults() == [], (
        "false-positive: תקלת strategy_tick_stalled נרשמה למרות שהלולאה בריאה "
        "(רק ה-wall-clock קפץ עקב שינת מערכת)"
    )


def test_genuine_stall_still_recorded():
    """תקיעה אמיתית: גם ה-heartbeat המונוטוני ישן (545s) → התקלה חייבת להירשם."""
    rt = main.runner.rt
    rt.mode = "auto"
    rt.last_tick_ts = time.time() - 545
    rt.last_tick_monotonic = time.monotonic() - 545  # הלולאה באמת חנוקה
    _run_one_watchdog_pass()
    assert len(_stall_faults()) >= 1, (
        "רגרסיה: תקיעת-tick אמיתית לא זוהתה — איבדנו את הזיהוי האמיתי"
    )
