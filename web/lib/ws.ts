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
        let data: any;
        try {
          data = JSON.parse(e.data);
        } catch (err) {
          // Silent failure here used to mask real bugs (server-side
          // serialization errors, accidental binary frames). Log so the
          // problem surfaces in devtools instead of vanishing.
          console.warn("ws: failed to parse frame", err, e.data);
          return;
        }
        if (data?.event) {
          try {
            handlerRef.current(data.event, data.payload);
          } catch (err) {
            console.error("ws: handler threw for event", data.event, err);
          }
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
