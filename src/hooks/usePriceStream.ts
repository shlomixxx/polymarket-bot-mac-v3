import { useCallback, useEffect, useRef, useState } from "react";
import { logClientEvent } from "../api";

export type SidePrice = {
  bid: number | null;
  ask: number | null;
  mid: number | null;
  ts: number;
};

export type PriceStreamData = {
  up: SidePrice | null;
  down: SidePrice | null;
  connected: boolean;
  lastUpdateTs: number;
};

const EMPTY: PriceStreamData = {
  up: null,
  down: null,
  connected: false,
  lastUpdateTs: 0,
};

function getWsUrl(): string {
  if (typeof window === "undefined") return "ws://127.0.0.1:8767/ws/prices";
  const host = window.location.hostname || "127.0.0.1";
  const port = window.location.port || "";
  const httpProtocol = window.location.protocol || "http:";
  const wsProtocol = httpProtocol === "https:" ? "wss:" : "ws:";

  if (!host || httpProtocol === "file:") return "ws://127.0.0.1:8767/ws/prices";
  if (port === "5175") return `ws://${host}:8767/ws/prices`;
  return `${wsProtocol}//${host}${port ? ":" + port : ""}/ws/prices`;
}

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;
// אחרי כמה זמן יציב נחשיב את החיבור "בריא" ונאפס את ה-backoff
const RECONNECT_RESET_AFTER_MS = 30000;

export function usePriceStream(): PriceStreamData {
  const [data, setData] = useState<PriceStreamData>(EMPTY);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const mountedRef = useRef(true);
  const reconnectDelayRef = useRef<number>(RECONNECT_BASE_MS);

  const openedAtRef = useRef<number>(0);
  const msgCountRef = useRef<number>(0);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    try {
      const ws = new WebSocket(getWsUrl());
      wsRef.current = ws;
      const openAttemptAt = Date.now();
      logClientEvent({ kind: "ws_connect_attempt", path: "/ws/prices", method: "WS" });

      ws.onopen = () => {
        if (mountedRef.current) {
          setData((prev) => ({ ...prev, connected: true }));
        }
        openedAtRef.current = Date.now();
        msgCountRef.current = 0;
        // איפוס מיידי של ה-backoff בכל פתיחה מוצלחת
        reconnectDelayRef.current = RECONNECT_BASE_MS;
        logClientEvent({
          kind: "ws_open",
          path: "/ws/prices",
          method: "WS",
          duration_ms: Date.now() - openAttemptAt,
        });
      };

      ws.onmessage = (ev) => {
        if (!mountedRef.current) return;
        msgCountRef.current += 1;
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "ping") return;
          if (msg.type === "price") {
            const side = msg.side as string;
            if (side !== "Up" && side !== "Down") return;
            const bid = typeof msg.bid === "number" ? msg.bid : null;
            const ask = typeof msg.ask === "number" ? msg.ask : null;
            // דחיית הודעות לא תקינות: bid > ask, מחירים מחוץ ל-[0,1], או שני הצדדים null.
            if (bid !== null && (bid < 0 || bid > 1)) return;
            if (ask !== null && (ask < 0 || ask > 1)) return;
            if (bid !== null && ask !== null && bid > ask) return;
            if (bid === null && ask === null) return;
            const priceData: SidePrice = {
              bid,
              ask,
              mid: typeof msg.mid === "number" ? msg.mid : null,
              ts: typeof msg.ts === "number" ? msg.ts : Date.now() / 1000,
            };
            setData((prev) => ({
              ...prev,
              [side === "Up" ? "up" : "down"]: priceData,
              lastUpdateTs: Date.now(),
            }));
          }
        } catch { /* ignore malformed */ }
      };

      ws.onclose = (ev) => {
        const liveMs = openedAtRef.current ? Date.now() - openedAtRef.current : 0;
        logClientEvent({
          kind: "ws_close",
          path: "/ws/prices",
          method: "WS",
          status: ev.code,
          duration_ms: liveMs,
          query: `messages=${msgCountRef.current}`,
        });
        if (mountedRef.current) {
          setData((prev) => ({ ...prev, connected: false }));
          // אם החיבור החזיק מספיק זמן, להתחיל מבסיס; אחרת להגדיל את ההמתנה.
          if (liveMs >= RECONNECT_RESET_AFTER_MS) {
            reconnectDelayRef.current = RECONNECT_BASE_MS;
          } else {
            reconnectDelayRef.current = Math.min(
              Math.max(reconnectDelayRef.current * 2, RECONNECT_BASE_MS),
              RECONNECT_MAX_MS,
            );
          }
          reconnectTimer.current = setTimeout(connect, reconnectDelayRef.current);
        }
      };

      ws.onerror = () => {
        logClientEvent({ kind: "ws_error", path: "/ws/prices", method: "WS", status: -1 });
        ws.close();
      };
    } catch {
      logClientEvent({ kind: "ws_construct_error", path: "/ws/prices", method: "WS", status: -1 });
      reconnectDelayRef.current = Math.min(
        Math.max(reconnectDelayRef.current * 2, RECONNECT_BASE_MS),
        RECONNECT_MAX_MS,
      );
      reconnectTimer.current = setTimeout(connect, reconnectDelayRef.current);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [connect]);

  return data;
}
