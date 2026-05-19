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

const DEFAULT_TIMEOUT_MS = 15_000;

/** גילוי שוק פעיל (Gamma) + מטא — עלול לחרוג מ־15s בקריאה קרה/עומס; המנוע עצמו מוגבל ב־wait_for נפרד */
export const TIMEOUT_MS_MARKET_CURRENT = 45_000;

/** orderbook-summary: גילוי + CLOB Up/Down — לעיתים >15s תחת עומס */
export const TIMEOUT_MS_ORDERBOOK_SUMMARY = 45_000;

/** demo/state: כולל mark_to_market — עלול להתארך כשהמנוע עסוק */
export const TIMEOUT_MS_DEMO_STATE = 45_000;

// ─── Request logger (client side) ──────────────────────────────────────────
// Every call through `api()` is captured and POSTed (batched) to the engine,
// which appends to engine/logs/requests.jsonl. Used to identify duplicate
// API calls. The sink endpoint itself is excluded to prevent recursion.

type ClientLogEntry = {
  ts: string;
  method: string;
  path: string;
  query: string;
  status: number;
  duration_ms: number;
  request_id?: string;
  caller_hint?: string;
  layout?: string;
  kind?: string;
};

const LOG_SINK_PATH = "/api/_log/client-request";
const LOG_FLUSH_MS = 1000;
const LOG_FLUSH_MAX = 50;

const _logQueue: ClientLogEntry[] = [];
let _flushScheduled = false;

function _flushClientLog(useBeacon: boolean): void {
  if (_logQueue.length === 0) return;
  const entries = _logQueue.splice(0, _logQueue.length);
  const url = `${BASE}${LOG_SINK_PATH}`;
  const body = JSON.stringify({ entries });
  if (useBeacon && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
    try {
      const blob = new Blob([body], { type: "application/json" });
      if (navigator.sendBeacon(url, blob)) return;
    } catch {
      /* fall through to fetch */
    }
  }
  // Fire-and-forget. Use raw fetch so we never recurse through `api()`.
  try {
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => { /* ignore */ });
  } catch {
    /* ignore */
  }
}

function _scheduleFlush(): void {
  if (_flushScheduled) return;
  _flushScheduled = true;
  setTimeout(() => {
    _flushScheduled = false;
    _flushClientLog(false);
  }, LOG_FLUSH_MS);
}

function _enqueueLog(entry: ClientLogEntry): void {
  _logQueue.push(entry);
  if (_logQueue.length >= LOG_FLUSH_MAX) {
    _flushClientLog(false);
  } else {
    _scheduleFlush();
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => _flushClientLog(true));
  window.addEventListener("beforeunload", () => _flushClientLog(true));
}

function _getCallerHint(): string {
  try {
    const stack = new Error().stack ?? "";
    const lines = stack.split("\n").map((s) => s.trim()).filter(Boolean);
    for (const line of lines) {
      if (line === "Error" || line.startsWith("Error:")) continue;
      // skip the api.ts frames themselves
      if (line.includes("/api.ts") || line.includes("api.ts:") || line.includes("api.ts?")) continue;
      // Chrome/Edge: "at funcName (url:line:col)"
      let m = line.match(/at\s+([^\s]+)\s+\((.+?):(\d+):(\d+)\)/);
      if (m) {
        const fn = m[1];
        const file = (m[2].split("/").pop() ?? m[2]).split("?")[0];
        return `${fn} @ ${file}:${m[3]}`;
      }
      // Chrome anonymous: "at url:line:col"
      m = line.match(/at\s+(.+?):(\d+):(\d+)$/);
      if (m) {
        const file = (m[1].split("/").pop() ?? m[1]).split("?")[0];
        return `${file}:${m[2]}`;
      }
      // Firefox/Safari: "fn@url:line:col"
      m = line.match(/^(.+?)@(.+?):(\d+):(\d+)$/);
      if (m) {
        const fn = m[1] || "(anon)";
        const file = (m[2].split("/").pop() ?? m[2]).split("?")[0];
        return `${fn} @ ${file}:${m[3]}`;
      }
      return line.length > 200 ? line.slice(0, 200) : line;
    }
  } catch {
    /* ignore */
  }
  return "unknown";
}

function _layout(): string {
  if (typeof window === "undefined") return "";
  return (window.location.hash || window.location.pathname || "").slice(0, 64);
}

function _emitClientLog(entry: ClientLogEntry): void {
  try {
    _enqueueLog(entry);
  } catch {
    /* never let logging crash anything */
  }
}

// In-flight GET dedup: when a GET to the same `method path` is already in progress,
// new callers share the existing Promise instead of opening a second fetch.
// This eliminates concurrent duplicates from overlapping component refresh()
// bundles. Once the request settles, the entry is removed — no post-settle
// caching, so callers always see fresh data after a request completes.
const _inflightGets = new Map<string, Promise<unknown>>();

async function _executeFetch<T>(
  path: string,
  fetchOpt: RequestInit,
  effectiveTimeout: number,
  callerHint: string | undefined,
  isLogSink: boolean,
  startMs: number,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, effectiveTimeout);

  let logStatus = 0;
  let logRequestId: string | undefined;

  try {
    const r = await fetch(`${BASE}${path}`, {
      ...fetchOpt,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...fetchOpt?.headers },
    });
    logStatus = r.status;
    logRequestId = r.headers.get("x-request-id") ?? undefined;
    if (!r.ok) {
      const t = await r.text();
      throw new Error(t || r.statusText);
    }
    return (await r.json()) as T;
  } catch (e) {
    if (timedOut || (e instanceof DOMException && e.name === "AbortError")) {
      if (logStatus === 0) logStatus = -2;
      throw new Error(`Timeout (${Math.round(effectiveTimeout / 1000)}s): ${path}`);
    }
    if (logStatus === 0) logStatus = -1;
    throw e;
  } finally {
    clearTimeout(timer);
    if (!isLogSink) {
      const nowMs = typeof performance !== "undefined" ? performance.now() : Date.now();
      const durationMs = Math.round((nowMs - startMs) * 10) / 10;
      const qIdx = path.indexOf("?");
      const pathOnly = qIdx >= 0 ? path.slice(0, qIdx) : path;
      const query = qIdx >= 0 ? path.slice(qIdx + 1) : "";
      _emitClientLog({
        ts: new Date().toISOString(),
        method: ((fetchOpt.method as string | undefined) ?? "GET").toUpperCase(),
        path: pathOnly,
        query,
        status: logStatus,
        duration_ms: durationMs,
        request_id: logRequestId,
        caller_hint: callerHint,
        layout: _layout(),
      });
    }
  }
}

export async function api<T>(path: string, opt?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const isLogSink = path.startsWith(LOG_SINK_PATH);
  const startMs = typeof performance !== "undefined" ? performance.now() : Date.now();
  const callerHint = isLogSink ? undefined : _getCallerHint();

  const { timeoutMs, ...fetchOpt } = opt ?? {};
  const effectiveTimeout = timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const method = ((fetchOpt.method as string | undefined) ?? "GET").toUpperCase();

  // Dedup only safe, idempotent GETs. Skip the log-sink to avoid recursion.
  if (method === "GET" && !isLogSink) {
    const key = `GET ${path}`;
    const existing = _inflightGets.get(key);
    if (existing) {
      const qIdx = path.indexOf("?");
      const pathOnly = qIdx >= 0 ? path.slice(0, qIdx) : path;
      const query = qIdx >= 0 ? path.slice(qIdx + 1) : "";
      _emitClientLog({
        ts: new Date().toISOString(),
        method: "GET",
        path: pathOnly,
        query,
        status: 0,
        duration_ms: 0,
        caller_hint: callerHint,
        layout: _layout(),
        kind: "deduped_inflight",
      });
      return existing as Promise<T>;
    }
    const work = _executeFetch<T>(path, fetchOpt, effectiveTimeout, callerHint, isLogSink, startMs);
    _inflightGets.set(key, work);
    void work.finally(() => {
      if (_inflightGets.get(key) === work) _inflightGets.delete(key);
    });
    return work;
  }

  return _executeFetch<T>(path, fetchOpt, effectiveTimeout, callerHint, isLogSink, startMs);
}

/** Emit a custom client log entry (e.g. WebSocket open/close events). */
export function logClientEvent(entry: Partial<ClientLogEntry> & { kind: string; path: string }): void {
  _emitClientLog({
    ts: new Date().toISOString(),
    method: entry.method ?? "EVENT",
    path: entry.path,
    query: entry.query ?? "",
    status: entry.status ?? 0,
    duration_ms: entry.duration_ms ?? 0,
    request_id: entry.request_id,
    caller_hint: entry.caller_hint ?? _getCallerHint(),
    layout: entry.layout ?? _layout(),
    kind: entry.kind,
  });
}

/** true when the tab/window is hidden — polls should back off. */
export function isPageHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function engineUrl(path: string) {
  return `${BASE}${path}`;
}
