function getApiBase() {
  // ב-production (Railway) ה-UI וה-API צריכים להיות מאותו origin.
  // ב-dev (Vite על 5175) ה-API נשאר על 8767.
  if (typeof window === "undefined") return "http://127.0.0.1:8767";
  const host = window.location.hostname || "127.0.0.1";
  const port = window.location.port || "";
  const protocol = window.location.protocol || "http:";

  // Electron/file://
  if (!host) return "http://127.0.0.1:8767";

  // אפליקציה מארוזה / index מקובץ — אין same-origin ל-API; המנוע תמיד על 8767
  if (protocol === "file:") {
    return "http://127.0.0.1:8767";
  }

  // Local dev via Vite
  if (port === "5175") {
    return `${protocol}//${host}:8767`;
  }

  // Deployed web (same domain, no hardcoded port)
  return "";
}

const BASE = getApiBase();

const DEFAULT_TIMEOUT_MS = 8_000;

export async function api<T>(path: string, opt?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs, ...fetchOpt } = opt ?? {};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs ?? DEFAULT_TIMEOUT_MS);
  try {
    const r = await fetch(`${BASE}${path}`, {
      ...fetchOpt,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...fetchOpt?.headers },
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(t || r.statusText);
    }
    return r.json() as Promise<T>;
  } finally {
    clearTimeout(timer);
  }
}

/** true when the tab/window is hidden — polls should back off. */
export function isPageHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function engineUrl(path: string) {
  return `${BASE}${path}`;
}
