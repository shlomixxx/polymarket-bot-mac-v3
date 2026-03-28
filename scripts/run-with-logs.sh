#!/usr/bin/env bash
# הרצת הבוט עם לוגים מלאים לתיקייה ממוינת לפי תאריך ושעת ההרצה.

set -euo pipefail

# macOS: לחיצה כפולה על .command פותחת טרמינל עם PATH מצומצם — בלי זה node/npm לא נמצאים
# והחלון נשאר "ריק" בלי Electron. (גם run-bot*.command משתמש ב-zsh -l לטעינת פרופיל.)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# Volta / asdf / fnm — נפוצים ב-macOS
if [[ -d "${HOME}/.volta" ]]; then
  export PATH="${HOME}/.volta/bin:${PATH}"
fi
if [[ -f "${HOME}/.asdf/asdf.sh" ]]; then
  # shellcheck disable=SC1090
  source "${HOME}/.asdf/asdf.sh" || true
fi
if command -v fnm >/dev/null 2>&1; then
  eval "$(fnm env)" 2>/dev/null || true
elif [[ -x "${HOME}/.local/share/fnm/fnm" ]]; then
  eval "$("${HOME}/.local/share/fnm/fnm" env)" 2>/dev/null || true
fi
if [[ -f "${HOME}/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1090
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  source "${HOME}/.nvm/nvm.sh" || true
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo ""
  echo "❌ לא נמצאו node או npm ב-PATH."
  echo "   פתח טרמינל רגיל והרץ:  cd \"$ROOT\" && npm run dev"
  echo "   או התקן Node מ־https://nodejs.org (או Homebrew: brew install node)"
  echo ""
  exit 1
fi

# Python למנוע — חייב uvicorn; פותרים אותו Python שבו התקנת pip (לעיתים 3.12 מ־python.org)
# בעוד ש־python3 ב־PATH הוא 3.13 מ־Homebrew בלי חבילות.
if ! ENGINE_PY="$(bash "$ROOT/scripts/resolve-engine-python.sh" 2>/dev/null)"; then
  echo ""
  echo "❌ לא נמצא Python עם חבילות המנוע (uvicorn). התקן עם install-engine-deps.command"
  echo "   או:  cd \"$ROOT/engine\" && python3 -m pip install -r requirements.txt"
  echo "   (במק נפוץ שני Pythonים — חשוב לאותו python שמריץ את uvicorn.)"
  echo ""
  exit 1
fi
export PYTHON_FOR_ENGINE="$ENGINE_PY"

DAY="$(date +%Y-%m-%d)"
TIME="$(date +%H-%M-%S)"
RUN_REL="logs/runs/${DAY}/${TIME}"
export LOG_RUN_DIR="${ROOT}/${RUN_REL}"
mkdir -p "$LOG_RUN_DIR"

export LOG_RUN_REL="${RUN_REL}"

# meta.json — JSON תקין (Python)
"${PYTHON_FOR_ENGINE}" - "$LOG_RUN_DIR" "$ROOT" "$RUN_REL" "$DAY" "$TIME" <<'PY'
import json, os, subprocess, sys, socket
from datetime import datetime, timezone

out_dir, root, run_rel, day, time_s = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
now = datetime.now(timezone.utc)
started_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
run_name = f"polymarket-bot {day} {time_s}"
meta = {
    "schema": "polymarket-bot-run-meta/v1",
    "run_name": run_name,
    "run_date": day,
    "run_time": time_s,
    "started_at_local": started_local,
    "started_at_utc": now.isoformat().replace("+00:00", "Z"),
    "run_relative_path": run_rel,
    "log_run_dir": out_dir,
    "cwd": root,
    "pid": os.getpid(),
    "user": os.environ.get("USER", ""),
    "hostname": socket.gethostname(),
}
try:
    meta["node"] = subprocess.check_output(["node", "-v"], text=True, cwd=root, timeout=5).strip()
except Exception:
    meta["node"] = "n/a"
try:
    meta["npm"] = subprocess.check_output(["npm", "-v"], text=True, cwd=root, timeout=5).strip()
except Exception:
    meta["npm"] = "n/a"
try:
    py = os.environ.get("PYTHON_FOR_ENGINE") or "python3"
    meta["python"] = subprocess.check_output([py, "--version"], text=True, cwd=root, timeout=5).strip()
except Exception:
    meta["python"] = "n/a"
try:
    meta["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=root, timeout=5).strip()
    meta["git_branch"] = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True, cwd=root, timeout=5).strip()
except Exception:
    pass
with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=2)
PY

touch "$LOG_RUN_DIR/combined.log"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "לוגים לריצה זו:  $LOG_RUN_DIR"
echo "  • run_info.txt        — שם הריצה, תאריך, אסטרטגיה (סקירה מהירה)"
echo "  • combined.log        — כל הפלט (מנוע + Vite + Electron)"
echo "  • meta.json           — שם ריצה, תאריך, זמן, סביבה, נתיב"
echo "  • strategy_journal.txt — אסטרטגיה+פרמטרים בהתחלה, ואז כל היומן (לניתוח)"
echo "  • engine_startup.json — snapshot בעליית המנוע"
echo "  • strategy_snapshot.json — עדכון כל ~60 שנ׳"
echo "  • journal_by_session.json — לוגים מקובצים לפי מחזור עסקה"
echo "  • journal_by_session.txt  — קריא לפי מחזור עסקה"
echo "  • trades.json             — כל העסקאות (מחזורים מלאים)"
echo "  • trades_summary.txt      — סיכום מחזורים: צד, TP/EXPIRE, PnL, שיא/שפל"
echo "  • run_diagnostics.txt     — אבחון: תקיעות, רווחים/הפסדים, פוזיציות"
echo "  • events.jsonl            — שינויי מצב / אירועים"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "לסגירה: CTRL+C"
echo ""
echo "Node: $(command -v node 2>/dev/null || echo '—')  $(node -v 2>/dev/null || true)"
echo "npm:  $(command -v npm 2>/dev/null || echo '—')  $(npm -v 2>/dev/null || true)"
echo "Python (מנוע): ${PYTHON_FOR_ENGINE}  $("${PYTHON_FOR_ENGINE}" --version 2>/dev/null || true)"
echo ""

END_META() {
  "${PYTHON_FOR_ENGINE}" - "$LOG_RUN_DIR" <<'PY'
import json, os, sys
from datetime import datetime, timezone
d = sys.argv[1]
end = {"schema": "polymarket-bot-run-end/v1", "ended_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "log_run_dir": d}
with open(os.path.join(d, "session_end.json"), "w", encoding="utf-8") as f:
    json.dump(end, f, ensure_ascii=False, indent=2)
PY
}

trap END_META EXIT INT TERM

export LOG_RUN_DIR
npm run dev 2>&1 | tee "$LOG_RUN_DIR/combined.log"
