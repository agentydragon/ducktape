import { fromJson } from "@bufbuild/protobuf";

import { WatchInboxResponseSchema, type Inbox } from "../../../notification_service/api/notifications/notifications_pb";
import { followWatch, type WatchHandlers } from "../transport/watch_stream";

export function followNotificationWatch(
  sessionId: string,
  resource: "inbox" | "inbox/entries" | "subscriptions",
  handlers: WatchHandlers
): () => void {
  return followWatch(`/upstream/notifications/v1/sessions/${encodeURIComponent(sessionId)}/${resource}:watch`, {
    ...handlers,
    initialFrameTimeoutMs: 5000,
    idleTimeoutMs: 45000,
  });
}

/** A fresh inbox state (including noInbox) must precede heartbeats on each connection. */
export function followInbox(
  sessionId: string,
  onState: (inbox: Inbox | null) => void,
  onConnection: WatchHandlers["onConnection"]
): () => void {
  let ready = false;
  return followNotificationWatch(sessionId, "inbox", {
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
