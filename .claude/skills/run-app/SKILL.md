---
name: run-app
description: Launch and drive THIS app (Polymarket BTC bot) locally on macOS — Python engine (:8767) + Vite UI (:5175) + Electron desktop window. Use when asked to run, start, open, or screenshot the app, or to verify a change works in the real desktop app.
---

# Running the Polymarket BTC bot locally (macOS)

The app is three processes launched together by `npm run dev` (via `concurrently -k`):

1. **Engine** — Python FastAPI on `http://127.0.0.1:8767` (`scripts/run-engine.sh` → uvicorn `main:app`)
2. **UI** — Vite dev server on `http://127.0.0.1:5175`
3. **Electron** — desktop window; `wait-on` blocks until both URLs above answer, then opens the window pointing at the Vite URL.

## THE ONE GOTCHA — `ELECTRON_RUN_AS_NODE`

When launched from inside another Electron app (Cursor, the Claude desktop app, VS Code), the parent exports `ELECTRON_RUN_AS_NODE=1`. This leaks into the child Electron and makes `require("electron")` return a **path string instead of the API object**, so `ipcMain`/`app`/`safeStorage` are `undefined` and `electron/main.cjs` crashes immediately:

```
electron/main.cjs:43  ipcMain.handle("secrets:save", ...)
TypeError: Cannot read properties of undefined (reading 'handle')
```

Because `concurrently -k` kills the group when Electron exits, the engine and Vite die too, so it looks like the whole thing failed. It didn't — only Electron tripped on the leaked env var.

**Fix: always launch with that variable unset.** A normal double-click of `run-bot.command` in Finder is unaffected; this only bites when *an agent/terminal inside an Electron parent* starts it.

## Launch (background, capture logs)

```bash
cd "<repo root>"
# clear any stale engine/vite first (safe if nothing is listening)
lsof -tiTCP:8767 -sTCP:LISTEN | xargs -r kill 2>/dev/null
lsof -tiTCP:5175 -sTCP:LISTEN | xargs -r kill 2>/dev/null

LOG=/tmp/polymarket-dev-run.log
env -u ELECTRON_RUN_AS_NODE npm run dev >"$LOG" 2>&1 &   # run in background
```

Python is auto-resolved by `scripts/resolve-engine-python.sh` — it picks the interpreter that actually has `uvicorn` (on this machine: `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`). Override with `PYTHON_FOR_ENGINE=/path/to/python3` if needed. If `uvicorn` import fails, run `bash scripts/install-engine-deps.sh`.

## Verify it actually came up (don't just launch — drive it)

```bash
# engine health — retries handle the ~15-30s WS/Chainlink startup
curl -s --retry 50 --retry-delay 1 --retry-all-errors --retry-connrefused http://127.0.0.1:8767/api/health   # -> {"ok":true}
curl -s -o /dev/null -w "vite %{http_code}\n" http://127.0.0.1:5175/                                          # -> 200
curl -s http://127.0.0.1:8767/api/faults | head -c 300                                                        # -> real fault data (the 🐞 תקלות tab)
# window really painted? look for renderer processes:
ps aux | grep "[e]lectron/dist/Electron.app" | grep -c Renderer                                               # -> >=1
# crash check in the log (should print nothing):
grep -iE "ipcMain|TypeError|exited with code [1-9]" "$LOG"
```

Window title is **"Polymarket BTC — גרסה 3"**. If it didn't come to the front, it's in the Dock / Cmd+Tab.

## Notes

- This is the **dev** build off local code (not the Railway server). UI hot-reloads.
- Default mode is **off** (no real trades) — safe to explore.
- **Stop everything:** close the window (kills engine+vite via `concurrently -k`), or `lsof -tiTCP:8767 -sTCP:LISTEN | xargs kill`.
