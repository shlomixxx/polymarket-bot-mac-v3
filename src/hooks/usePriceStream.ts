import { useCallback, useEffect, useRef, useState } from "react";

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

export function usePriceStream(): PriceStreamData {
  const [data, setData] = useState<PriceStreamData>(EMPTY);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const mountedRef = useRef(true);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    try {
      const ws = new WebSocket(getWsUrl());
      wsRef.current = ws;

      ws.onopen = () => {
        if (mountedRef.current) {
          setData((prev) => ({ ...prev, connected: true }));
        }
      };

      ws.onmessage = (ev) => {
        if (!mountedRef.current) return;
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "ping") return;
          if (msg.type === "price") {
            const side = msg.side as string;
            const priceData: SidePrice = {
              bid: msg.bid ?? null,
              ask: msg.ask ?? null,
              mid: msg.mid ?? null,
              ts: msg.ts ?? Date.now() / 1000,
            };
            setData((prev) => ({
              ...prev,
              [side === "Up" ? "up" : "down"]:
                side === "Up" || side === "Down" ? priceData : prev[side === "Up" ? "up" : "down"],
              lastUpdateTs: Date.now(),
            }));
          }
        } catch { /* ignore malformed */ }
      };

      ws.onclose = () => {
        if (mountedRef.current) {
          setData((prev) => ({ ...prev, connected: false }));
          reconnectTimer.current = setTimeout(connect, 1500);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch {
      reconnectTimer.current = setTimeout(connect, 2000);
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
