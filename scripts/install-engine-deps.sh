#!/usr/bin/env bash
# התקנת requirements למנוע — אותו סדר Python כמו ב־resolve-engine-python.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/engine" || exit 1

if [[ ! -f requirements.txt ]]; then
  echo "❌ לא נמצא engine/requirements.txt"
  exit 1
fi

if PY="$(bash "$ROOT/scripts/resolve-engine-python.sh" 2>/dev/null)"; then
  echo "משתמש ב־Python (כבר יש uvicorn): $PY"
else
  if PY="$(bash "$ROOT/scripts/resolve-engine-python.sh" --install-target 2>/dev/null)"; then
    echo "מתקין ל־Python (יעד ברירת מחדל): $PY"
  else
    PY="python3"
    echo "משתמש ב־python3 מה־PATH: $(command -v python3 2>/dev/null || echo לא נמצא)"
  fi
fi

echo ""
"$PY" --version
echo ""
echo "מתקין חבילות מ־requirements.txt ..."
echo ""
"$PY" -m pip install -r requirements.txt

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ סיום. אם לא היו שגיאות למעלה — הרץ שוב: run-bot-with-logs.command"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
printf '%s' "לחץ Enter לסגירה... "
read -r
