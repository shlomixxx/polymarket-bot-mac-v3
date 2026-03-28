#!/bin/zsh
# הפעלת הבוט תמיד עם לוגים מלאים — ממוינים לפי תאריך, שעה ומחזור עסקה.

cd "$(dirname "$0")" || exit 1
chmod +x ./scripts/run-with-logs.sh 2>/dev/null || true
export POLYMARKET_BOT_MAC_V3_ROOT="$(pwd)"
export POLYMARKET_BOT_DISABLE_AUTO_START="1"
exec /bin/zsh -l -c 'cd "$POLYMARKET_BOT_MAC_V3_ROOT" && exec ./scripts/run-with-logs.sh'

