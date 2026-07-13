import { useEffect, useRef, useState } from "react";

// The live channel's health, surfaced so the shell can show when it's broken (a dead socket
// means the approvals panel only updates on reload — the operator must be told, not left
// silently stale). `connecting` is the pre-open grace state (no alarm during the handshake);
// `offline` persists across reconnect attempts until one succeeds, so the indicator doesn't
// blink between backoff tries.
export type LiveStatus = "connecting" | "live" | "offline";

export type ConsoleEvent = { event_type: string };

// The authoritative server-pushed event signal shared by console surfaces. `/api/events/ws`
// carries tool-call and operator-link changes across replicas via Postgres LISTEN/NOTIFY.
// `onEvent` fires once on mount (initial read), on every event, and again on reconnect (to
// catch up on anything missed while the socket was down). The returned `LiveStatus` lets the
// shell chrome flag a dead channel.
export function useConsoleEvents(onEvent: (event: ConsoleEvent) => void): LiveStatus {
  // Held in a ref so a changing callback identity never re-opens the socket.
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  });

  const [status, setStatus] = useState<LiveStatus>("connecting");

  useEffect(() => {
    const sync = () => onEventRef.current({ event_type: "sync" });
    sync(); // initial read; the WS below only delivers subsequent changes

    // Only a real web origin can open the WS. A screenshot harness renders from an origin-less
    // about:blank page, where the initial read above already populated the view and
    // `new WebSocket` on a non-ws URL would throw — so skip it there (status stays `connecting`,
    // which shows no alarm).
    const { protocol, href } = window.location;
    if (protocol !== "https:" && protocol !== "http:") return;

    const url = new URL("/api/events/ws", href);
    url.protocol = protocol === "https:" ? "wss:" : "ws:";

    let closed = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let backoffMs = 1000;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(url);
      ws.onopen = () => {
        if (closed) return;
        backoffMs = 1000;
        setStatus("live");
        sync(); // catch up on any events missed while the socket was down
      };
      ws.onmessage = (message) => {
        if (closed) return;
        try {
          const event: unknown = JSON.parse(String(message.data));
          if (!event || typeof event !== "object" || !("event_type" in event) || typeof event.event_type !== "string") {
            throw new Error("console event is missing event_type");
          }
          onEventRef.current(event as ConsoleEvent);
        } catch (error) {
          console.error("Invalid console event", error);
        }
      };
      // onerror always precedes onclose; let onclose drive the state + reconnect so both a
      // failed handshake and a dropped connection funnel through one path.
      ws.onclose = () => {
        if (closed) return;
        setStatus("offline");
        sync(); // one REST refetch so a momentary blip still refreshes the view
        reconnectTimer = window.setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, 30_000);
      };
    };
    connect();

    return () => {
      closed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);

  return status;
}
