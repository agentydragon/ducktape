import { fromJson } from "@bufbuild/protobuf";

import { WatchInboxResponseSchema, type Inbox } from "../../../notification_service/api/notifications/notifications_pb";
import { followWatch, type WatchHandlers } from "../transport/watch_stream";

function watchStream(sessionId: string, resource: string, query: string, handlers: WatchHandlers): () => void {
  return followWatch(`/upstream/notifications/v1/sessions/${encodeURIComponent(sessionId)}/${resource}:watch${query}`, {
    ...handlers,
    initialFrameTimeoutMs: 5000,
    idleTimeoutMs: 45000,
  });
}

export function followNotificationWatch(
  sessionId: string,
  resource: "inbox/entries" | "subscriptions",
  handlers: WatchHandlers
): () => void {
  return watchStream(sessionId, resource, "", handlers);
}

/**
 * A fresh inbox state (including noInbox) must precede heartbeats on each connection. The watch sends a frame only
 * when one of `fields` (`Inbox` field paths) changes, and the frame carries only them and the inbox's identity.
 */
export function followInbox(
  sessionId: string,
  fields: readonly string[],
  onState: (inbox: Inbox | null) => void,
  onConnection: WatchHandlers["onConnection"]
): () => void {
  let ready = false;
  return watchStream(sessionId, "inbox", `?${new URLSearchParams({ field_mask: fields.join(",") })}`, {
    onOpen: () => {
      ready = false;
    },
    onConnection,
    onFrame: (json) => {
      const { state } = fromJson(WatchInboxResponseSchema, json);
      if (state.case === "heartbeat") {
        if (!ready) throw new Error("Inbox heartbeat before initial state");
        return;
      }
      if (state.case === undefined) throw new Error("Inbox watch returned no state");
      ready = true;
      onState(state.case === "inbox" ? state.value : null);
    },
  });
}
