#!/usr/bin/env bash
# מוצא python שבו מותקן uvicorn (למנוע מצב שבו pip התקין ל־3.12 מ־python.org
# והבוט רץ עם python3 מ־Homebrew בלי חבילות).
# מדפיס נתיב אחד ל־stdout. קוד יציאה 1 אם לא נמצא.
# אפשר לעקוף: PYTHON_FOR_ENGINE=/path/to/python3
#
# שימוש:
#   resolve-engine-python.sh              — קודם Python עם uvicorn
#   resolve-engine-python.sh --install-target — נתיב להתקנת pip (ראשון קיים ברשימה), בלי לבדוק uvicorn

set -euo pipefail

fill_candidates() {
  candidates=()
  for v in 3.13 3.12 3.11 3.10; do
    p="/Library/Frameworks/Python.framework/Versions/${v}/bin/python3"
    [[ -x "$p" ]] && candidates+=("$p")
  done
  if [[ -x "/Library/Frameworks/Python.framework/Versions/Current/bin/python3" ]]; then
    candidates+=("/Library/Frameworks/Python.framework/Versions/Current/bin/python3")
  fi
  for p in \
    /opt/homebrew/opt/python@3.12/bin/python3 \
    /opt/homebrew/opt/python@3.13/bin/python3.13 \
    /opt/homebrew/opt/python@3.13/bin/python3 \
    /usr/local/opt/python@3.12/bin/python3 \
    /usr/local/bin/python3 \
    /opt/homebrew/bin/python3; do
    [[ -x "$p" ]] && candidates+=("$p")
  done
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi
}

try_python() {
  local real="$1"
  [[ -z "$real" ]] && return 1
  [[ -x "$real" ]] || return 1
  if "$real" -c "import uvicorn" 2>/dev/null; then
    printf '%s' "$real"
    return 0
  fi
  return 1
}

INSTALL_ONLY=0
if [[ "${1:-}" == "--install-target" ]]; then
  INSTALL_ONLY=1
fi

fill_candidates

if [[ "$INSTALL_ONLY" -eq 1 ]]; then
  seen="|"
  for p in "${candidates[@]}"; do
    [[ -z "$p" ]] && continue
    case "$seen" in
      *"|${p}|"*) continue ;;
    esac
    seen="${seen}${p}|"
    if [[ -x "$p" ]]; then
      printf '%s' "$p"
      exit 0
    fi
  done
  exit 1
fi

# עקיפה ידנית (חייב להיות עם uvicorn)
if [[ -n "${PYTHON_FOR_ENGINE:-}" ]]; then
  if command -v "${PYTHON_FOR_ENGINE}" >/dev/null 2>&1; then
    REAL="$(command -v "${PYTHON_FOR_ENGINE}")"
  elif [[ -x "${PYTHON_FOR_ENGINE}" ]]; then
    REAL="${PYTHON_FOR_ENGINE}"
  else
    REAL=""
  fi
  if [[ -n "$REAL" ]] && try_python "$REAL"; then
    exit 0
  fi
fi

seen="|"
for p in "${candidates[@]}"; do
  [[ -z "$p" ]] && continue
  case "$seen" in
    *"|${p}|"*) continue ;;
  esac
  seen="${seen}${p}|"
  if try_python "$p"; then
    exit 0
  fi
done

exit 1
