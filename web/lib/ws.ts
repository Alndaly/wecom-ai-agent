"use client";
import { useEffect, useRef } from "react";
import { WS_BASE, getToken, refreshAccessToken } from "./api";

type Handler = (event: string, payload: any) => void;

export function useWebWs(onEvent: Handler) {
  const ref = useRef<WebSocket | null>(null);
  const handlerRef = useRef(onEvent);

  useEffect(() => {
    handlerRef.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    let stopped = false;
    let pingTimer: ReturnType<typeof setInterval> | null = null;

    const connect = () => {
      const token = getToken();
      if (!token) return;
      const ws = new WebSocket(`${WS_BASE}/ws/web?token=${encodeURIComponent(token)}`);
      ref.current = ws;
      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.event) handlerRef.current(data.event, data.payload);
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
      ws.onclose = async (event) => {
        if (pingTimer) clearInterval(pingTimer);
        if (stopped) return;
        if (event.code === 4401) {
          const refreshed = await refreshAccessToken();
          if (!refreshed) return;
        }
        setTimeout(connect, 2000);
      };
    };
    connect();
    return () => {
      stopped = true;
      if (pingTimer) clearInterval(pingTimer);
      ref.current?.close();
    };
  }, []);
}
