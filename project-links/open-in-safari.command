#!/bin/zsh
# פותח את רשימת הקישורים ב-Safari, אחרי שרת מקומי קטן (כמו בכרום — ללא תלות ב-file://).
# Safari אין לו מצב --app כמו בכרום; ראו הסבר ב-index.html: מסך מלא או "הוסף ל-Dock".

cd "$(dirname "$0")" || exit 1
PORT="${PROJECT_LINKS_HTTP_PORT:-9473}"
URL="http://127.0.0.1:${PORT}/index.html"

if command -v lsof >/dev/null; then
  pids=$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null)
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null
fi

nohup python3 -m http.server "${PORT}" --bind 127.0.0.1 --directory "$PWD" \
  >>"${TMPDIR:-/tmp}/project-links-http.log" 2>&1 &
disown 2>/dev/null || true
sleep 0.45

if [[ -d "/Applications/Safari.app" ]]; then
  open -a Safari "$URL"
  exit 0
fi

echo "Safari לא נמצא."
open "$URL"
