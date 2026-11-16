/** Live per-Session inbox state; reading it never acknowledges an entry. */
import { Badge, Tooltip } from "@mantine/core";
import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";
import { useEffect, useState, type JSX } from "react";

import { clockTime } from "../format_time";
import { followWatch } from "../watch_stream";
import { WatchInboxResponseSchema, type Inbox } from "../../../notification_service/api/notifications/notifications_pb";

export interface ThreadNotices {
  bySession: Map<string, Inbox>;
  live: Set<string>;
}

export function inboxWatchUrl(sessionId: string): string {
  return `/upstream/notifications/v1/sessions/${encodeURIComponent(sessionId)}/inbox:watch`;
}

export function useThreadNotices(threadIds: readonly string[]): ThreadNotices {
  const key = [...new Set(threadIds)].sort().join("\n");
  const [state, setState] = useState<ThreadNotices>(() => ({ bySession: new Map(), live: new Set() }));
  useEffect(() => {
    const ids = key === "" ? [] : key.split("\n");
    setState({ bySession: new Map(), live: new Set() });
    const stops = ids.map((id) => {
      let receivedSnapshot = false;
      return followWatch(inboxWatchUrl(id), {
        initialFrameTimeoutMs: 5000,
        idleTimeoutMs: 45000,
        onOpen: () => {
          receivedSnapshot = false;
        },
        onFrame: (json) => {
          const response = fromJson(WatchInboxResponseSchema, json as JsonValue);
          if (response.state.case === "heartbeat") {
            if (!receivedSnapshot) throw new Error("Inbox heartbeat before initial state");
            return;
          }
          if (response.state.case !== "inbox" && response.state.case !== "noInbox")
            throw new Error("Inbox watch returned no state");
          receivedSnapshot = true;
          setState((old) => {
            const bySession = new Map(old.bySession);
            if (response.state.case === "inbox") bySession.set(id, response.state.value);
            else bySession.delete(id);
            return { ...old, bySession };
          });
        },
        onConnection: (connection) =>
          setState((old) => {
            const live = new Set(old.live);
            if (connection.phase === "live") live.add(id);
            else live.delete(id);
            return { ...old, live };
          }),
      });
    });
    return () => {
      for (const stop of stops) stop();
    };
  }, [key]);
  return state;
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

export function ThreadNoticeBadge({ inbox, live }: { inbox?: Inbox; live: boolean }): JSX.Element | null {
  if (!inbox) return null;
  const queued = inbox.delivery?.unannouncedCount ?? 0;
  const pending = inbox.delivery?.pendingAcknowledgementCount ?? 0;
  const unacknowledged = Math.max(0, pending - queued);
  const error = inbox.latestNotice?.error ?? inbox.deliveryError;
  if (error === undefined && pending === 0) return null;
  const sending = unacknowledged > 0 && !inbox.latestNotice?.confirmed;
  const eligible = inbox.delivery?.noticeDueAt
    ? (toJson(TimestampSchema, inbox.delivery.noticeDueAt) as string)
    : undefined;
  const lines = [
    error === undefined ? null : `Delivery failed: ${error}`,
    unacknowledged === 0
      ? null
      : sending
        ? `${plural(unacknowledged, "notification")} being delivered to the agent`
        : `${plural(unacknowledged, "notification")} delivered, not yet acknowledged by the agent`,
    queued === 0
      ? null
      : `${plural(queued, "notification")} queued for the next notice` +
        (eligible === undefined ? "" : `, due around ${clockTime(eligible)} (estimate; new events push it back)`),
    live ? null : "Notification status disconnected; this may be out of date.",
  ].filter((line): line is string => line !== null);
  return (
    <Tooltip label={lines.join("\n")} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <Badge
        className="agentplane-thread-list-row-notices"
        data-state={error === undefined ? "pending" : "error"}
        variant="filled"
        color={error === undefined ? "gray.5" : "red.4"}
        aria-label={lines.join(". ")}
        style={live ? undefined : { opacity: 0.5 }}
      >
        {pending === 0 ? "!" : pending}
      </Badge>
    </Tooltip>
  );
}
