import { useCallback, useEffect, useRef } from "react";

// The console's "tool-call state may have changed, re-read it" signal, shared by every
// surface that shows tool calls (the approval drawer and the full history view). It fuses
// two sources: the server-pushed `/api/approvals/ws` WebSocket (a submit/approve/deny/finish
// happened somewhere) and a cross-tab BroadcastChannel (another console tab made a change).
// `onEvent` fires once on mount (initial read) and again on every event; `notifyPeers()`
// lets a tab that just changed state wake its siblings.
const TOOL_CALL_CHANNEL = "haku-console-tool-approvals";

export function useToolCallEvents(onEvent: () => void): { notifyPeers: () => void } {
  // Held in a ref so a changing callback identity never re-opens the socket/channel.
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  });

  const channelRef = useRef<BroadcastChannel | null>(null);

  useEffect(() => {
    const fire = () => onEventRef.current();
    fire(); // initial read; the WS/channel below only deliver subsequent changes

    let channel: BroadcastChannel | null = null;
    if ("BroadcastChannel" in window) {
      channel = new BroadcastChannel(TOOL_CALL_CHANNEL);
      channelRef.current = channel;
      channel.onmessage = () => fire();
    }

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
      if (channelRef.current === channel) channelRef.current = null;
      channel?.close();
    };
  }, []);

  return { notifyPeers: useCallback(() => channelRef.current?.postMessage({ type: "toolCallsChanged" }), []) };
}
