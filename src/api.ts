function getApiBase() {
  // ב-production (Railway) ה-UI וה-API צריכים להיות מאותו origin.
  // ב-dev (Vite על 5175) ה-API נשאר על 8767.
  if (typeof window === "undefined") return "http://127.0.0.1:8767";
  const host = window.location.hostname || "127.0.0.1";
  const port = window.location.port || "";
  const protocol = window.location.protocol || "http:";

  // Electron/file://
  if (!host) return "http://127.0.0.1:8767";

  // Local dev via Vite
  if (port === "5175") {
    return `${protocol}//${host}:8767`;
  }

  // Deployed web (same domain, no hardcoded port)
  return "";
}

const BASE = getApiBase();

export async function api<T>(path: string, opt?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    ...opt,
    headers: { "Content-Type": "application/json", ...opt?.headers },
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || r.statusText);
  }
  return r.json() as Promise<T>;
}

export function engineUrl(path: string) {
  return `${BASE}${path}`;
}
