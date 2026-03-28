#!/bin/zsh
# לחיצה כפולה: מתקין חבילות מנוע (אותו לוגיקת Python כמו run-bot-with-logs.command).

cd "$(dirname "$0")" || exit 1
chmod +x ./scripts/install-engine-deps.sh ./scripts/resolve-engine-python.sh 2>/dev/null || true
export POLYMARKET_BOT_MAC_V3_ROOT="$(pwd)"
exec /bin/zsh -l -c 'cd "$POLYMARKET_BOT_MAC_V3_ROOT" && exec bash ./scripts/install-engine-deps.sh'
