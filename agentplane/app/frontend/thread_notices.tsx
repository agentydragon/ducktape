/**
 * Per-thread notification state in the Thread list: what waits for a notice, what a notice
 * delivered that the agent has not acknowledged yet, and whether delivery failed.
 *
 * Read-only. Following the stream acknowledges nothing; only the agent acknowledges its inbox.
 * Every snapshot is the Notification Service's current state, so a reconnect needs no resume.
 */
import { Badge, Tooltip } from "@mantine/core";
import { type JSX, useEffect, useState } from "react";

import { followStream, type StreamConnection } from "./live_stream";

/** `notification_service.models.InboxNoticeState`. */
export type InboxNoticeState = {
  inbox_id: string;
  session_id: string;
  queued: number;
  unacknowledged: number;
  notice: "prepared" | "attempted" | "admitted" | "confirmed" | null;
  notice_wait_reason: string | null;
  eligible_at: string | null;
  error: string | null;
};

type Snapshot = { observed_at: string; inboxes: InboxNoticeState[] };

/** The route's own bound, as many threads as one listing page carries. */
const MAX_THREADS = 200;

export interface ThreadNotices {
  /** By Session ID, which is the thread's ID; a thread with no live inbox has no entry. Null until
   * the first snapshot. */
  bySession: Map<string, InboxNoticeState[]> | null;
  live: boolean;
}

export function threadNoticesUrl(threadIds: readonly string[]): string {
  const query = new URLSearchParams();
  for (const id of threadIds.slice(0, MAX_THREADS)) query.append("thread_id", id);
  return `/threads/notices/stream?${query}`;
}

/** Follows the notification state of `threadIds`, newest first; past the route's bound the oldest
 * go without. */
export function useThreadNotices(threadIds: readonly string[]): ThreadNotices {
  const url = threadIds.length === 0 ? null : threadNoticesUrl(threadIds);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [connection, setConnection] = useState<StreamConnection | null>(null);
  useEffect(() => {
    if (url === null) return;
    return followStream(url, {
      events: { snapshot: (message) => setSnapshot(JSON.parse(message.data) as Snapshot) },
      onConnection: setConnection,
    });
  }, [url]);
  if (url === null || snapshot === null) return { bySession: null, live: false };
  const bySession = new Map<string, InboxNoticeState[]>();
  for (const inbox of snapshot.inboxes)
    bySession.set(inbox.session_id, [...(bySession.get(inbox.session_id) ?? []), inbox]);
  return { bySession, live: connection?.phase === "live" };
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function clock(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** A thread row's badge: red when delivery failed, blue for delivered entries the agent has not
 * acknowledged, gray for entries still waiting for a notice; nothing once everything is
 * acknowledged. */
export function ThreadNoticeBadge({
  inboxes,
  live,
}: {
  inboxes: readonly InboxNoticeState[];
  live: boolean;
}): JSX.Element | null {
  const queued = inboxes.reduce((sum, inbox) => sum + inbox.queued, 0);
  const unacknowledged = inboxes.reduce((sum, inbox) => sum + inbox.unacknowledged, 0);
  const error = inboxes.find((inbox) => inbox.error !== null)?.error ?? null;
  if (error === null && queued === 0 && unacknowledged === 0) return null;
  const sending = inboxes.some((inbox) => inbox.unacknowledged > 0 && inbox.notice !== "confirmed");
  const eligible = inboxes
    .map((inbox) => inbox.eligible_at)
    .filter((at): at is string => at !== null)
    .sort()[0];
  const lines = [
    error === null ? null : `Delivery failed: ${error}`,
    unacknowledged === 0
      ? null
      : sending
        ? `${plural(unacknowledged, "notification")} being delivered to the agent`
        : `${plural(unacknowledged, "notification")} delivered, not yet acknowledged by the agent`,
    queued === 0
      ? null
      : `${plural(queued, "notification")} queued for the next notice` +
        (eligible === undefined ? "" : `, due around ${clock(eligible)} (estimate; new events push it back)`),
    live ? null : "Notification status disconnected; this may be out of date.",
  ].filter((line): line is string => line !== null);
  const [color, count] =
    error !== null ? ["red", "!"] : unacknowledged > 0 ? ["blue", unacknowledged] : ["gray", queued];
  return (
    <Tooltip label={lines.join("\n")} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <Badge
        className="agentplane-sidebar-row-notices"
        data-state={error !== null ? "error" : unacknowledged > 0 ? "delivered" : "queued"}
        size="xs"
        circle
        variant={unacknowledged > 0 || error !== null ? "filled" : "light"}
        color={color}
        aria-label={lines.join(". ")}
        style={live ? undefined : { opacity: 0.5 }}
      >
        {count}
      </Badge>
    </Tooltip>
  );
}
