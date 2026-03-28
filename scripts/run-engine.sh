#!/usr/bin/env bash
# מופעל מ־npm run engine — משתמש ב־PYTHON_FOR_ENGINE אם הוגדר, אחרת פותר אוטומטית.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/engine"

PY="${PYTHON_FOR_ENGINE:-}"
if [[ -z "$PY" ]]; then
  if resolved="$(bash "$ROOT/scripts/resolve-engine-python.sh" 2>/dev/null)"; then
    PY="$resolved"
  else
    PY="python3"
  fi
fi

exec "$PY" -m uvicorn main:app --host 127.0.0.1 --port 8767
