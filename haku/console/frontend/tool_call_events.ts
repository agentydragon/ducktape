import { useEffect, useRef } from "react";

// The console's "tool-call state may have changed, re-read it" signal, shared by every
// surface that shows tool calls (the approval drawer and the full history view). The
// authoritative source is the server-pushed `/api/approvals/ws` WebSocket: the backend's
// ToolCallEventHub broadcasts every submit/approve/deny/finish to every connected tab
// (across replicas via Postgres LISTEN/NOTIFY), so a change in one tab reaches the others
// through the server. `onEvent` fires once on mount (initial read) and again on every event.
export function useToolCallEvents(onEvent: () => void): void {
  // Held in a ref so a changing callback identity never re-opens the socket.
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  });

  useEffect(() => {
    const fire = () => onEventRef.current();
    fire(); // initial read; the WS below only delivers subsequent changes

    let closed = false;
    let ws: WebSocket | null = null;
    // Only a real web origin can open the WS. A screenshot harness renders from an
    // origin-less about:blank page, where the initial read above already populated the view
    // and `new WebSocket` on a non-ws URL would throw — so skip it there.
    const { protocol, href } = window.location;
    if (protocol === "https:" || protocol === "http:") {
      const url = new URL("/api/approvals/ws", href);
      url.protocol = protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(url);
      const fireIfLive = () => {
        if (!closed) fire();
      };
      ws.onopen = fireIfLive;
      ws.onmessage = fireIfLive;
      ws.onclose = fireIfLive;
    }

    return () => {
      closed = true;
      ws?.close();
    };
  }, []);
}
