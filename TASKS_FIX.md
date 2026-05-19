# TASKS_FIX — תיקוני יציבות וזמן אמת

**מטרה**: לוודא שהפרויקט רץ ללא בעיות, הנתונים יהיו כמה שיותר קרובים לזמן אמת,
ושמסחר אמיתי יעבוד בצורה אמינה.

---

## CRITICAL

- [ ] **#1 כתיבה אטומית של state JSON** — `engine/demo_engine.py:save()` + כל קובץ persisted אחר. tmp+rename.
- [ ] **#2 watchdog ל-WS Polymarket** — אם אין הודעה > 30s → לדווח stale, להריץ HTTP fallback לפני החלטות מסחר.
- [ ] **#3 ולידציה של fill price אמיתי אחרי order** — לא להסתמך על `lo.get("price")` שזה ה-limit cap.
- [ ] **#4 drop-by-token ב-WS broadcast** — להחליף הודעה ישנה של אותו טוקן במקום drop-oldest שרירותי.

## HIGH

- [ ] **#5 exponential backoff ב-frontend** — `usePriceStream.ts` reconnect (1s→2s→4s→8s→max 15s).
- [ ] **#6 bid>ask validation ב-frontend** — דחיית הודעות לא תקינות.
- [ ] **#7 background tasks wrapper** — try/except + restart loop במקום קריסה שקטה.
- [ ] **#8 timeout מפורש על create_and_post_order** — `asyncio.wait_for(..., 10s)`.
- [ ] **#9 reconcile מיידי בכשל balance** — לא להמתין 120s.

## MEDIUM (נדחה לפעם הבאה אם הזמן לא מספיק)

- [ ] WS unsubscribe לטוקנים ישנים אחרי rollover.
- [ ] thread-safety ל-caches (klines, signals).
- [ ] cache של min_order_size — TTL קצר יותר אחרי rollover.

---

## בדיקות לאחר תיקון

1. `python -m py_compile engine/*.py` — חייב לעבור.
2. `npx tsc --noEmit` — typecheck של ה-frontend.
3. `python -m pytest engine/tests/` — אם יש tests.
4. הפעלת `scripts/run-engine.sh` ב-DRY_RUN ולעקוב 30s אחר logs.

## סיום

5. `git add` + commit מסכם + push למקור.
6. בדיקת Railway: לוודא ש-deployment חדש עולה והשירות בריא.
