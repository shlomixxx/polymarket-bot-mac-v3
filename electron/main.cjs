const { app, BrowserWindow, ipcMain, safeStorage } = require("electron");
const path = require("path");
const { spawn } = require("child_process");

let mainWindow;
let engineProc;

function startEngine() {
  const engineDir = path.join(__dirname, "..", "engine");
  engineProc = spawn("python3", ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8767"], {
    cwd: engineDir,
    env: { ...process.env },
    stdio: "pipe",
  });
  engineProc.stdout?.on("data", (d) => console.log("[engine]", d.toString().trim()));
  engineProc.stderr?.on("data", (d) => console.error("[engine]", d.toString().trim()));
  engineProc.on("exit", (code) => console.log(`[engine] יצא עם קוד ${code}`));
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      // מאפשר Web Audio / צלילי שידור בלי לחיצה ראשונה (בדפדפן עדיין נדרש unlock)
      autoplayPolicy: "no-user-gesture-required",
    },
    title: "Polymarket BTC — גרסה 3",
  });
  const isDev = !app.isPackaged;
  if (isDev) {
    mainWindow.loadURL("http://127.0.0.1:5175");
    mainWindow.webContents.openDevTools({ mode: "detach" });
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

ipcMain.handle("secrets:save", (_e, key, value) => {
  try {
    if (safeStorage.isEncryptionAvailable()) {
      const buf = safeStorage.encryptString(value);
      return { ok: true, data: buf.toString("base64") };
    }
    return { ok: false, error: "אחסון מוצפן לא זמין" };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
});

ipcMain.handle("secrets:load", (_e, b64) => {
  try {
    if (!b64) return { ok: true, value: "" };
    const buf = Buffer.from(b64, "base64");
    if (safeStorage.isEncryptionAvailable()) {
      return { ok: true, value: safeStorage.decryptString(buf) };
    }
    return { ok: false, error: "decrypt" };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
});

app.whenReady().then(() => {
  if (!app.isPackaged) {
    /* בפיתוח: המנוע רץ מ-npm run engine */
  } else {
    startEngine();
  }
  setTimeout(createWindow, app.isPackaged ? 1500 : 300);
});

app.on("window-all-closed", () => {
  if (engineProc) engineProc.kill();
  app.quit();
});
