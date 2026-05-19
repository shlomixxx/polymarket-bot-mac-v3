#!/usr/bin/env bash
# מופעל מ־npm run engine — משתמש ב־PYTHON_FOR_ENGINE אם הוגדר, אחרת פותר אוטומטית.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/engine"

# טיק אסטרטגיה מהיר (ברירת מחדל 0.1s — עקיפה: STRATEGY_TICK_SLEEP_SEC=0.08)
export STRATEGY_TICK_SLEEP_SEC="${STRATEGY_TICK_SLEEP_SEC:-0.1}"

PY="${PYTHON_FOR_ENGINE:-}"
if [[ -z "$PY" ]]; then
  if resolved="$(bash "$ROOT/scripts/resolve-engine-python.sh" 2>/dev/null)"; then
    PY="$resolved"
  else
    PY="python3"
  fi
fi

exec "$PY" -m uvicorn main:app --host 127.0.0.1 --port 8767
