#!/usr/bin/env bash
# אוסף חבילת אבחון לשיתוף עם קלוד — מסנן סודות ושומר קובץ אחד שאפשר לגרור לצ'אט.
#
# שימוש:
#   ./scripts/share-logs.sh                 — ריצה אחרונה, אחרי סינון סודות
#   ./scripts/share-logs.sh 2026-04-14/12-30-00  — ריצה ספציפית
#   ./scripts/share-logs.sh --tail 2000    — רק 2000 שורות אחרונות מ-combined.log (ברירת מחדל: 1500)
#
# פלט: logs/shared/bundle-YYYY-MM-DD-HH-MM-SS.txt  (עובר סינון סודות)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUNS_DIR="${ROOT}/logs/runs"
OUT_DIR="${ROOT}/logs/shared"
mkdir -p "$OUT_DIR"

TAIL_LINES=1500
TARGET_REL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tail)
      TAIL_LINES="${2:-1500}"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [YYYY-MM-DD/HH-MM-SS] [--tail N]"
      exit 0
      ;;
    *)
      TARGET_REL="$1"
      shift
      ;;
  esac
done

if [[ -z "$TARGET_REL" ]]; then
  # ריצה אחרונה: התיקייה האחרונה לפי תאריך, ובתוכה התיקייה האחרונה לפי שעה
  if [[ ! -d "$RUNS_DIR" ]]; then
    echo "❌ אין תיקיית לוגים: $RUNS_DIR"
    exit 1
  fi
  LAST_DAY="$(ls -1 "$RUNS_DIR" 2>/dev/null | sort | tail -n 1 || true)"
  if [[ -z "$LAST_DAY" || ! -d "$RUNS_DIR/$LAST_DAY" ]]; then
    echo "❌ אין תיקיות תאריך בתוך $RUNS_DIR"
    exit 1
  fi
  LAST_TIME="$(ls -1 "$RUNS_DIR/$LAST_DAY" 2>/dev/null | sort | tail -n 1 || true)"
  if [[ -z "$LAST_TIME" ]]; then
    echo "❌ אין תיקיות ריצה בתוך $RUNS_DIR/$LAST_DAY"
    exit 1
  fi
  TARGET_REL="$LAST_DAY/$LAST_TIME"
fi

RUN_DIR="$RUNS_DIR/$TARGET_REL"
if [[ ! -d "$RUN_DIR" ]]; then
  echo "❌ תיקיית הריצה לא קיימת: $RUN_DIR"
  exit 1
fi

STAMP="$(date +%Y-%m-%d-%H-%M-%S)"
OUT_FILE="$OUT_DIR/bundle-$STAMP.txt"

echo "📦 אוסף חבילת לוגים מ:"
echo "   $RUN_DIR"
echo "   → $OUT_FILE"
echo ""

# פונקציית סינון סודות — רגקס לערכים שנראים כמו מפתחות פרטיים/API/כתובות ארנק.
#   - 0x... 64-char hex (private key / 32-byte hash)
#   - כתובות Ethereum (0x + 40 hex): בשימוש חוקי — משאירים אבל מחליפים רק מפתחות פרטיים
#   - דפוסים ספציפיים: POLYMARKET_PRIVATE_KEY, api_key, secret, passphrase
filter_secrets() {
  # sed: בשלב ראשון נקה env-style assignments; אח"כ החלף hex של 64 תווים (מפתח פרטי)
  sed -E \
    -e 's#(POLYMARKET_PRIVATE_KEY)[[:space:]]*=[[:space:]]*"?[^"[:space:]]+"?#\1=***REDACTED***#gI' \
    -e 's#("?(?:private_?key|api[_-]?key|api[_-]?secret|apiSecret|apiKey|passphrase|secret|signer_key)"?[[:space:]]*[:=][[:space:]]*")[^"]+(")#\1***REDACTED***\2#gI' \
    -e 's#\b0x[a-fA-F0-9]{64}\b#0x***REDACTED_PRIVATE_KEY_64***#g'
}

{
  echo "================================================================"
  echo "POLYMARKET BOT — DIAGNOSTIC BUNDLE"
  echo "Run: $TARGET_REL"
  echo "Generated: $STAMP"
  echo "Tail lines (combined.log): $TAIL_LINES"
  echo "================================================================"
  echo ""

  # --- meta.json ---
  if [[ -f "$RUN_DIR/meta.json" ]]; then
    echo "## meta.json"
    echo '```json'
    filter_secrets < "$RUN_DIR/meta.json"
    echo '```'
    echo ""
  fi

  # --- run_diagnostics.txt (אם קיים — האבחון האוטומטי) ---
  if [[ -f "$RUN_DIR/run_diagnostics.txt" ]]; then
    echo "## run_diagnostics.txt"
    echo '```'
    filter_secrets < "$RUN_DIR/run_diagnostics.txt"
    echo '```'
    echo ""
  fi

  # --- trades_summary.txt ---
  if [[ -f "$RUN_DIR/trades_summary.txt" ]]; then
    echo "## trades_summary.txt"
    echo '```'
    filter_secrets < "$RUN_DIR/trades_summary.txt"
    echo '```'
    echo ""
  fi

  # --- trades.json ---
  if [[ -f "$RUN_DIR/trades.json" ]]; then
    echo "## trades.json"
    echo '```json'
    filter_secrets < "$RUN_DIR/trades.json"
    echo '```'
    echo ""
  fi

  # --- events.jsonl (500 שורות אחרונות) ---
  if [[ -f "$RUN_DIR/events.jsonl" ]]; then
    echo "## events.jsonl (last 500 lines)"
    echo '```'
    tail -n 500 "$RUN_DIR/events.jsonl" | filter_secrets
    echo '```'
    echo ""
  fi

  # --- strategy_snapshot.json ---
  if [[ -f "$RUN_DIR/strategy_snapshot.json" ]]; then
    echo "## strategy_snapshot.json"
    echo '```json'
    filter_secrets < "$RUN_DIR/strategy_snapshot.json"
    echo '```'
    echo ""
  fi

  # --- engine_startup.json ---
  if [[ -f "$RUN_DIR/engine_startup.json" ]]; then
    echo "## engine_startup.json"
    echo '```json'
    filter_secrets < "$RUN_DIR/engine_startup.json"
    echo '```'
    echo ""
  fi

  # --- combined.log (tail) ---
  if [[ -f "$RUN_DIR/combined.log" ]]; then
    echo "## combined.log (last $TAIL_LINES lines)"
    echo '```'
    tail -n "$TAIL_LINES" "$RUN_DIR/combined.log" | filter_secrets
    echo '```'
    echo ""
  fi

  echo "================================================================"
  echo "END OF BUNDLE"
  echo "================================================================"
} > "$OUT_FILE"

SIZE_KB=$(( $(wc -c < "$OUT_FILE") / 1024 ))

echo "✅ נוצר: $OUT_FILE  (${SIZE_KB}KB)"
echo ""
echo "📋 לשיתוף עם קלוד:"
echo "   1. גרור את הקובץ לחלון הצ'אט (drag-and-drop), או:"
echo "   2. הרץ:  open \"$OUT_FILE\""
echo ""
echo "🔒 סינון: POLYMARKET_PRIVATE_KEY, api_key, api_secret, passphrase, 0x...64chars → REDACTED"
echo "   ⚠️  בדוק שוב ידנית לפני שיתוף! (לעיתים סוד חדש מגיע במבנה לא צפוי.)"

# פתיחה אוטומטית ב-Finder (ב-macOS)
if command -v open >/dev/null 2>&1; then
  open -R "$OUT_FILE" 2>/dev/null || true
fi
