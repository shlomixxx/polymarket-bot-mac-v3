#!/bin/zsh
# פותח את דף הקישורים ב-Chrome/Edge במצב "אפליקציה" — בלי טאבים ולרוב בלי סרגל כתובות.
# עובד דרך שרת HTTP מקומי (לא file://) כי כרום לעיתים משאיר סרגל על קבצים מקומיים.
#
# לחיצה כפולה מה-Finder. פורט ברירת מחדל: 9473 — אפשר לשנות: export PROJECT_LINKS_HTTP_PORT=9474

cd "$(dirname "$0")" || exit 1
PORT="${PROJECT_LINKS_HTTP_PORT:-9473}"
URL="http://127.0.0.1:${PORT}/index.html"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

# שחרור פורט אם נתפס מריצה קודמת
if command -v lsof >/dev/null; then
  pids=$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null)
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null
fi

nohup python3 -m http.server "${PORT}" --bind 127.0.0.1 --directory "$PWD" \
  >>"${TMPDIR:-/tmp}/project-links-http.log" 2>&1 &
disown 2>/dev/null || true
sleep 0.45

chrome_like_app() {
  local bin="$1"
  "$bin" \
    --app="$URL" \
    --disable-infobars \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    >/dev/null 2>&1 &
}

if [[ -x "$CHROME" ]]; then
  chrome_like_app "$CHROME"
  exit 0
fi
if [[ -x "$EDGE" ]]; then
  chrome_like_app "$EDGE"
  exit 0
fi

echo "לא נמצא Chrome או Edge ב־Applications. פותחים בדפדפן ברירת המחדל (עם סרגל)."
open "$URL"
