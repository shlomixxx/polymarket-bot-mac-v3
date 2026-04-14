#!/bin/zsh
# מסך מלא "קיוסק" — בלי סרגלי דפדפן כמעט בכלל. יוצאים עם ⌘Q (או Alt+F4 בחלונות).
# משתמש באותו שרת מקומי כמו open-as-app.command (פורט 9473).

cd "$(dirname "$0")" || exit 1
PORT="${PROJECT_LINKS_HTTP_PORT:-9473}"
URL="http://127.0.0.1:${PORT}/index.html"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

if command -v lsof >/dev/null; then
  pids=$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null)
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null
fi

nohup python3 -m http.server "${PORT}" --bind 127.0.0.1 --directory "$PWD" \
  >>"${TMPDIR:-/tmp}/project-links-http.log" 2>&1 &
disown 2>/dev/null || true
sleep 0.45

if [[ -x "$CHROME" ]]; then
  "$CHROME" --kiosk "$URL" --no-first-run >/dev/null 2>&1 &
  exit 0
fi
if [[ -x "$EDGE" ]]; then
  "$EDGE" --kiosk "$URL" --no-first-run >/dev/null 2>&1 &
  exit 0
fi

echo "לא נמצא Chrome/Edge. פותחים כתובת רגילה."
open "$URL"
