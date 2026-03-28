#!/bin/zsh
# לחיצה כפולה ב-Finder: פותח טרמינל ומריץ את הבוט עם לוגים מלאים למבנה תיקיות.
#
# חשוב: טרמינל שנפתח מ-Finder לא טוען .zprofile/.zshrc — לכן node/npm/fnm לעיתים חסרים
# וה-Electron לא עולה. exec עם zsh -l (login shell) טוען את הפרופיל המלא.

cd "$(dirname "$0")" || exit 1
chmod +x ./scripts/run-with-logs.sh 2>/dev/null || true
export POLYMARKET_BOT_MAC_V3_ROOT="$(pwd)"
export POLYMARKET_BOT_DISABLE_AUTO_START="1"
exec /bin/zsh -l -c 'cd "$POLYMARKET_BOT_MAC_V3_ROOT" && exec ./scripts/run-with-logs.sh'
