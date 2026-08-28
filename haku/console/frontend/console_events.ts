import { useEffect, useRef, useState } from "react";

import { redirectToOperatorLogin } from "./operator_login";

// The one close the shell acts on instead of reconnecting: the operator session reached its
// absolute deadline, so every reconnect would be refused until the browser re-authenticates.
// Mirrors `OPERATOR_SESSION_EXPIRED_CLOSE_CODE` in ../notifications/console_events.py.
const OPERATOR_SESSION_EXPIRED_CLOSE_CODE = 4001;

// The live channel's health, surfaced so the shell can show when it's broken: a dead socket means
// the approvals panel only updates on reload. `connecting` is the pre-open grace state (no alarm
// during the handshake); `offline` persists across reconnect attempts until one succeeds, so the
// indicator doesn't blink between backoff tries.
export type LiveStatus = "connecting" | "live" | "offline";

export type ConsoleEvent = { event_type: string };

/** The conversation a `conversation_changed` event invalidates, or null for every other event.
 *
 * Mirrors `ConversationChangedEvent` in ../notifications/console_events.py, which carries no more than this: the
 * socket says a conversation changed and REST stays the source of what it changed to.
 *
 * Every consumer sees *every* event, and a streaming turn emits one of these per coalescing
 * window, so anything not about conversations should skip an event this returns an id for.
 */
export function changedConversationId(event: ConsoleEvent): string | null {
  if (event.event_type !== "conversation_changed") return null;
  const { conversation_id: conversationId } = event as ConsoleEvent & { conversation_id?: unknown };
  return typeof conversationId === "string" ? conversationId : null;
}

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
    // LISTEN/NOTIFY is an invalidation fast path, not a durable queue. A bounded catch-up keeps the
    // REST-backed view correct even across an undetected half-open DB connection or a failed NOTIFY.
    const syncTimer = window.setInterval(sync, 30_000);

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
      ws.onclose = (event) => {
        if (closed) return;
        // An expired session is not a channel outage: reconnecting would just be refused, and a
        // crossed-wifi indicator would blame the network for an auth problem.
        if (event.code === OPERATOR_SESSION_EXPIRED_CLOSE_CODE) {
          redirectToOperatorLogin();
          return;
        }
        setStatus("offline");
        sync(); // one REST refetch so a momentary blip still refreshes the view
        reconnectTimer = window.setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, 30_000);
      };
    };
    connect();

    return () => {
      closed = true;
      window.clearInterval(syncTimer);
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);

  return status;
}
