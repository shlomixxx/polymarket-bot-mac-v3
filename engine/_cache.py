"""תשתית cache משותפת (משימה 0.1 ב-API_RESOURCE_TASKS.md).

שני כלים קטנים שמחליפים caches אד-הוק פזורים:
  • TTLCache    — cache מבוסס-זמן עם get/set/invalidate/prune ותמיכה ב-now מפורש (לטסטים).
  • SingleFlight — ממזג קריאות אסינכרוניות *מקבילות* לאותו מפתח לבקשה אחת (de-dup),
                   בלי לאחסן את התוצאה אחרי שהסתיימה (לא cache — רק coalescing).

עקרון Guardrail: שני אלה הם שכבת תצוגה/יעילות בלבד. אסור לעטוף בהם קריאת מחיר/יתרה
ברגע ביצוע הזמנה (entry/exit/settlement) — ראה רשימת ה-Guardrails הקדושים בקובץ המשימות.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Hashable, Optional


class TTLCache:
    """cache פשוט מבוסס-זמן. get מחזיר ערך תקף בתוך ה-TTL, אחרת None."""

    def __init__(self, ttl_sec: float) -> None:
        self.ttl_sec = float(ttl_sec)
        self._store: dict[Hashable, tuple[float, Any]] = {}

    def get(self, key: Hashable, *, now: Optional[float] = None) -> Any:
        now = time.time() if now is None else now
        ent = self._store.get(key)
        if ent is None:
            return None
        ts, val = ent
        if now - ts > self.ttl_sec:
            return None
        return val

    def set(self, key: Hashable, value: Any, *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._store[key] = (now, value)

    def invalidate(self, key: Optional[Hashable] = None) -> None:
        """מבטל מפתח בודד, או הכל כש-key=None."""
        if key is None:
            self._store.clear()
        else:
            self._store.pop(key, None)

    def prune(self, *, now: Optional[float] = None, max_entries: Optional[int] = None) -> None:
        """מסיר רשומות שפג תוקפן; אופציונלית מגביל את הגודל (מסיר את הישנות ביותר)."""
        now = time.time() if now is None else now
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self.ttl_sec]
        for k in expired:
            self._store.pop(k, None)
        if max_entries is not None and len(self._store) > max_entries:
            # שמור את ה-max_entries העדכניים ביותר לפי חותמת הזמן
            ordered = sorted(self._store.items(), key=lambda kv: kv[1][0], reverse=True)
            self._store = dict(ordered[:max_entries])

    def __len__(self) -> int:
        return len(self._store)


class SingleFlight:
    """ממזג קריאות אסינכרוניות מקבילות לאותו מפתח לבקשה אחת.

    אינו cache: ברגע שהבקשה מסתיימת (הצלחה או כישלון) הרשומה מתנקה, כך שקריאה חדשה
    מפעילה מחדש את ה-factory. כך כשל לעולם לא "נדבק".
    """

    def __init__(self) -> None:
        self._inflight: dict[Hashable, "asyncio.Task[Any]"] = {}

    async def do(self, key: Hashable, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        existing = self._inflight.get(key)
        if existing is not None:
            # קורא נוסף שהגיע בזמן שהבקשה רצה — מחכה לאותה משימה.
            return await asyncio.shield(existing)

        # גם הקורא הראשון מחכה למשימה המשותפת, כך שחריגה תמיד נאספת (ללא אזהרת asyncio).
        task: "asyncio.Task[Any]" = asyncio.ensure_future(coro_factory())
        self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            # מנקים רק אם זו עדיין המשימה שלנו (לא דרסה אותה בקשה חדשה).
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
