"""בדיקה שהלוק של ה-rollover (FIX #24 v2) באמת מגן: שני _tick מקבילים לא ירוצו במקביל."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest


@pytest.mark.asyncio
async def test_rollover_lock_sentinel_prevents_double_settlement():
    """Tick A מתחיל rollover, Tick B מנסה במקביל — B צריך לחזור מיד בלי קריאה כפולה
    ל-expire_all_outside_tokens.

    הוכחה: נספור כמה פעמים expire_all_outside_tokens נקרא. צריך להיות 1.
    """
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import demo_engine
        importlib.reload(demo_engine)
        import strategy_runner
        importlib.reload(strategy_runner)

        eng = demo_engine.DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=eng)
        runner.rt.mode = "semi"  # mode != off → _tick לא יחזור מיד
        runner.rt.current_epoch = 1000  # epoch ישן

        # נדמה market discovery — מחזיר epoch חדש (1300)
        from dataclasses import dataclass

        @dataclass
        class FakeMarket:
            epoch: int = 1300
            slug: str = "btc-updown-5m-1300"
            token_up: str = "tok_up"
            token_down: str = "tok_down"
            window_sec: int = 300
            order_min_size: float = 5.0

        fake_m = FakeMarket()
        expire_call_count = 0

        async def fake_expire(*args, **kwargs):
            nonlocal expire_call_count
            expire_call_count += 1
            await asyncio.sleep(0.05)  # מדמה work שלוקח זמן
            return []

        # patch כל מה שצריך כדי שה-tick יגיע ל-rollover block
        with patch.object(
            runner._venue,
            "discover_active_window",
            new=AsyncMock(return_value=fake_m),
        ), patch.object(
            eng, "expire_all_outside_tokens", side_effect=fake_expire
        ), patch.object(
            runner, "_live_reconcile_if_enabled", new=AsyncMock(return_value=None)
        ), patch.object(
            runner._venue,
            "best_bid_ask",
            new=AsyncMock(return_value=(None, None)),
        ):
            # שני ticks מקבילים
            results = await asyncio.gather(
                runner._tick(), runner._tick(), return_exceptions=True
            )

        # אחרי ששניהם הסתיימו — expire נקרא בדיוק פעם אחת!
        assert expire_call_count == 1, (
            f"expire_all_outside_tokens נקרא {expire_call_count} פעמים — מצופה 1. "
            f"זה אומר שה-lock לא מגן בפועל."
        )
        # current_epoch התעדכן
        assert runner.rt.current_epoch == 1300


@pytest.mark.asyncio
async def test_rollover_lock_sequential_ticks_work_normally():
    """ודאו שטיק רגיל לא נחסם — הלוק לא משנה את ההתנהגות הרגילה כשאין race."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = str(d)
        import importlib
        import demo_engine
        importlib.reload(demo_engine)
        import strategy_runner
        importlib.reload(strategy_runner)

        eng = demo_engine.DemoEngine(Path(d) / "state.json")
        runner = strategy_runner.StrategyRunner(demo=eng)
        runner.rt.mode = "semi"
        runner.rt.current_epoch = 1000

        from dataclasses import dataclass

        @dataclass
        class FakeMarket:
            epoch: int = 1300
            slug: str = "btc-updown-5m-1300"
            token_up: str = "tok_up"
            token_down: str = "tok_down"
            window_sec: int = 300
            order_min_size: float = 5.0

        expire_count = 0

        async def fake_expire(*args, **kwargs):
            nonlocal expire_count
            expire_count += 1
            return []

        with patch.object(
            runner._venue,
            "discover_active_window",
            new=AsyncMock(return_value=FakeMarket()),
        ), patch.object(
            eng, "expire_all_outside_tokens", side_effect=fake_expire
        ), patch.object(
            runner, "_live_reconcile_if_enabled", new=AsyncMock(return_value=None)
        ), patch.object(
            runner._venue,
            "best_bid_ask",
            new=AsyncMock(return_value=(None, None)),
        ):
            await runner._tick()
            # tick שני — current_epoch כבר עודכן ל-1300, לא יהיה rollover שני
            await runner._tick()

        # rollover רץ פעם אחת בלבד (השני זיהה שאין שינוי epoch)
        assert expire_count == 1
        assert runner.rt.current_epoch == 1300
