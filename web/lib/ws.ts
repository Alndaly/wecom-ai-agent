"use client";
import { useEffect, useRef } from "react";
import { WS_BASE, getToken } from "./api";

type Handler = (event: string, payload: any) => void;

export function useWebWs(onEvent: Handler) {
  const ref = useRef<WebSocket | null>(null);
  useEffect(() => {
    const token = getToken();
    if (!token) return;
    let stopped = false;
    let pingTimer: ReturnType<typeof setInterval> | null = null;

    const connect = () => {
      const ws = new WebSocket(`${WS_BASE}/ws/web?token=${encodeURIComponent(token)}`);
      ref.current = ws;
      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.event) onEvent(data.event, data.payload);
        } catch {
          /* ignore */
        }
      };
      ws.onopen = () => {
        pingTimer = setInterval(() => {
          try {
            ws.send(JSON.stringify({ op: "ping" }));
          } catch {}
        }, 30_000);
      };
      ws.onclose = () => {
        if (pingTimer) clearInterval(pingTimer);
        if (!stopped) setTimeout(connect, 2000);
      };
    };
    connect();
    return () => {
      stopped = true;
      if (pingTimer) clearInterval(pingTimer);
      ref.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
