/**
 * Per-thread notification state in the Thread list: what waits for a notice, what a notice
 * delivered that the agent has not acknowledged yet, and whether delivery failed.
 *
 * Read-only. Following the stream acknowledges nothing; only the agent acknowledges its inbox.
 * Every snapshot is the Notification Service's current state, so a reconnect needs no resume.
 */
import { Badge, Tooltip } from "@mantine/core";
import type { JSX } from "react";

import { clockTime } from "../format_time";
import { useSnapshotStream } from "../live_stream";
import { decodeSessionNoticeStatesFrame, type InboxNoticeState, type SessionNoticeStates } from "../notification_types";

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
  const { snapshot, connection } = useSnapshotStream<SessionNoticeStates>(url, decodeSessionNoticeStatesFrame);
  if (url === null || snapshot === null) return { bySession: null, live: false };
  const bySession = new Map<string, InboxNoticeState[]>();
  for (const inbox of snapshot.inboxes)
    bySession.set(inbox.sessionId, [...(bySession.get(inbox.sessionId) ?? []), inbox]);
  return { bySession, live: connection.phase === "live" };
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** A thread row's badge: how many inbox entries the agent has not yet acknowledged, whether still
 * waiting for a notice or delivered by one; soft red while delivery is failing; nothing once the
 * inboxes are empty. */
export function ThreadNoticeBadge({
  inboxes,
  live,
}: {
  inboxes: readonly InboxNoticeState[];
  live: boolean;
}): JSX.Element | null {
  const queued = inboxes.reduce((sum, inbox) => sum + inbox.queued, 0);
  const unacknowledged = inboxes.reduce((sum, inbox) => sum + inbox.unacknowledged, 0);
  const error = inboxes.find((inbox) => inbox.error !== undefined)?.error;
  const unknownProgress = inboxes.find(({ notice }) => typeof notice === "object")?.notice;
  const unknownProgressValue = typeof unknownProgress === "object" ? unknownProgress.unknown : undefined;
  if (error === undefined && queued === 0 && unacknowledged === 0) return null;
  const sending = inboxes.some((inbox) => inbox.unacknowledged > 0 && inbox.notice !== "NOTICE_PROGRESS_CONFIRMED");
  const eligible = inboxes
    .map((inbox) => inbox.eligibleAt)
    .filter((at): at is string => at !== undefined)
    .sort()[0];
  const lines = [
    error === undefined ? null : `Delivery failed: ${error}`,
    unknownProgressValue === undefined ? null : `Unrecognized notification delivery state ${unknownProgressValue}`,
    unacknowledged === 0
      ? null
      : unknownProgressValue !== undefined
        ? `${plural(unacknowledged, "notification")} with an unrecognized delivery state`
        : sending
          ? `${plural(unacknowledged, "notification")} being delivered to the agent`
          : `${plural(unacknowledged, "notification")} delivered, not yet acknowledged by the agent`,
    queued === 0
      ? null
      : `${plural(queued, "notification")} queued for the next notice` +
        (eligible === undefined ? "" : `, due around ${clockTime(eligible)} (estimate; new events push it back)`),
    live ? null : "Notification status disconnected; this may be out of date.",
  ].filter((line): line is string => line !== null);
  const pending = queued + unacknowledged;
  const hasError = error !== undefined || unknownProgressValue !== undefined;
  return (
    <Tooltip label={lines.join("\n")} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <Badge
        className="agentplane-thread-list-row-notices"
        data-state={hasError ? "error" : "pending"}
        variant="filled"
        // Muted: the count is the agent's backlog, not a call on the operator's attention.
        color={hasError ? "red.4" : "gray.5"}
        aria-label={lines.join(". ")}
        style={live ? undefined : { opacity: 0.5 }}
      >
        {pending === 0 ? "!" : pending}
      </Badge>
    </Tooltip>
  );
}
