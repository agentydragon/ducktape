import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { dispatchFromJson } from "./client";
import { HttpError } from "../transport/client";
import { sessionDispatcherClient } from "../transport/session_dispatcher_client";
import { WatchStreamError } from "../transport/watch_stream";
import { canonicalDispatchCommandIds, type LocalCommands } from "./local_commands";
import {
  DeliveryState,
  RecentDispatchIssuesSchema,
  type DispatchIssue,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";

/** `DispatchFilter.command_ids`' bound; the commands past it go unwatched. */
const MAX_WATCHED_COMMANDS = 1000;
const RECENT_ISSUE_LIMIT = 20;

export type RecentDispatchIssues = {
  receipts: readonly DispatchIssue[];
  moreAvailable: boolean;
  connection: "connecting" | "current" | "stale";
  coverageLost: boolean;
};

export const EMPTY_DISPATCH_ISSUES: RecentDispatchIssues = {
  receipts: [],
  moreAvailable: false,
  connection: "connecting",
  coverageLost: false,
};

function readFrame(result: JsonValue): {
  dispatch?: JsonValue;
  caughtUp?: JsonValue;
  recentIssues?: JsonValue;
  resumeToken: string;
} {
  if (typeof result !== "object" || result === null || Array.isArray(result)) {
    throw new Error("Dispatcher returned an invalid watch frame");
  }
  const frame = result;
  if (typeof frame.resumeToken !== "string" || frame.resumeToken === "") {
    throw new Error("Dispatcher watch frame omitted its resume token");
  }
  return { ...frame, resumeToken: frame.resumeToken };
}

/** Clean EOF also ends a normal watch lease. Give the first retry time to catch up. */
const CLEAN_CLOSE_GRACE_MS = 3_000;
const INITIAL_CATCH_UP_TIMEOUT_MS = 60_000;

function outOfRange(reason: unknown): boolean {
  if (reason instanceof WatchStreamError) return reason.code === 11;
  return reason instanceof HttpError && reason.code === 11;
}

function saveCursor(store: LocalCommands, sessionId: string, commandIds: readonly string[], token: string): void {
  if (!store.saveDispatchWatchResumeToken(sessionId, commandIds, token)) {
    throw new Error("Dispatcher watch progress could not be saved locally");
  }
}

/** Keeps `store` current with the Dispatcher's rows for `commandIds` of one Session,
 * through the App's plain Dispatcher proxy. Returns the function that stops it. */
export function watchDispatches(store: LocalCommands, sessionId: string, commandIds: readonly string[]): () => void {
  const asked = canonicalDispatchCommandIds(commandIds).slice(0, MAX_WATCHED_COMMANDS);
  let resumeToken = store.dispatchWatchResumeToken(sessionId, asked);
  // A fresh snapshot is only complete at its first CaughtUp. Until then, don't persist per-row
  // cursors: if the stream ends partway through, the next connection must repeat the snapshot.
  let snapshotPending = resumeToken === null;
  const query = new URLSearchParams({ "filter.sessionId": sessionId });
  for (const id of asked) query.append("filter.commandIds", id);
  const nextQuery = () => {
    const params = new URLSearchParams(query);
    if (resumeToken !== null) params.set("resumeToken", resumeToken);
    return params;
  };
  let held = new Set<string>();
  let cleanCloseTimer: number | undefined;
  const clearCleanCloseTimer = () => {
    window.clearTimeout(cleanCloseTimer);
    cleanCloseTimer = undefined;
  };
  const stopWatch = sessionDispatcherClient.watchDispatches(nextQuery, {
    initialFrameTimeoutMs: INITIAL_CATCH_UP_TIMEOUT_MS,
    initialFrameReady: (frame) => typeof frame === "object" && frame !== null && "caughtUp" in frame,
    onOpen: () => {
      held = new Set();
    },
    onFrame: (result) => {
      try {
        const frame = readFrame(result);
        if (frame.dispatch !== undefined) {
          const receipt = dispatchFromJson(frame.dispatch);
          if (snapshotPending) held.add(receipt.commandId);
          // The row reaches durable storage before its token can advance. A failed write ends this
          // connection so the next attempt replays from the last durable cursor.
          if (!store.updateDispatch(receipt.commandId, receipt)) {
            throw new Error("Dispatcher state could not be saved locally");
          }
          if (!snapshotPending) {
            saveCursor(store, sessionId, asked, frame.resumeToken);
            resumeToken = frame.resumeToken;
          }
        } else if (frame.caughtUp !== undefined) {
          if (snapshotPending) {
            // Only a completed fresh snapshot can infer deletion from an absent row. Resumed and
            // recurring checkpoints carry changes only, so absence there says nothing.
            for (const commandId of asked.filter((id) => !held.has(id))) {
              const missing = dispatchFromJson({
                commandId,
                sessionId,
                state: "DELIVERY_STATE_UNKNOWN",
                reason: "sandbox_deleted",
              });
              if (!store.updateDispatch(commandId, missing)) {
                throw new Error("Missing Dispatcher state could not be saved locally");
              }
            }
          }
          saveCursor(store, sessionId, asked, frame.resumeToken);
          resumeToken = frame.resumeToken;
          snapshotPending = false;
          clearCleanCloseTimer();
          store.setDispatchWatchError(null);
        } else {
          throw new Error("Dispatcher returned a watch frame without a result");
        }
      } catch (reason) {
        console.warn("a dispatch frame could not be applied", reason);
        throw reason;
      }
    },
    onConnection: (connection) => {
      if (connection.phase !== "reconnecting") return;
      if (connection.lastError !== null) {
        clearCleanCloseTimer();
        store.setDispatchWatchError(connection.lastError);
      } else if (store.getSnapshot().dispatchWatchError === null) {
        cleanCloseTimer ??= window.setTimeout(
          () => store.setDispatchWatchError("The Dispatcher closed the stream."),
          CLEAN_CLOSE_GRACE_MS
        );
      }
    },
    onError: (reason) => {
      if (!outOfRange(reason)) return;
      store.clearDispatchWatchResumeToken(sessionId, asked);
      resumeToken = null;
      snapshotPending = true;
      held = new Set();
      store.recordDispatchWatchCoverageLoss();
    },
  });
  return () => {
    stopWatch();
    clearCleanCloseTimer();
  };
}

/** Discover Dispatcher issues for this Session, without writing browser submissions or History. */
export function watchRecentDispatchIssues(
  sessionId: string,
  publish: (state: RecentDispatchIssues) => void
): () => void {
  let resumeToken: string | null = null;
  let state = EMPTY_DISPATCH_ISSUES;
  const query = () => {
    const params = new URLSearchParams({
      "filter.sessionId": sessionId,
      "filter.recentIssueLimit": String(RECENT_ISSUE_LIMIT),
    });
    if (resumeToken !== null) params.set("resumeToken", resumeToken);
    return params;
  };
  return sessionDispatcherClient.watchDispatches(query, {
    onOpen: () => undefined,
    onFrame: (result) => {
      const frame = readFrame(result);
      const previous = state;
      if (frame.recentIssues !== undefined) {
        const snapshot = fromJson(RecentDispatchIssuesSchema, frame.recentIssues);
        if (
          snapshot.receipts.length > RECENT_ISSUE_LIMIT ||
          snapshot.receipts.some(
            (row) =>
              !row.commandId ||
              row.sessionId !== sessionId ||
              (row.state !== DeliveryState.FAILED && row.state !== DeliveryState.UNKNOWN)
          )
        ) {
          throw new Error("Dispatcher returned invalid recent delivery issues");
        }
        const received = new Set<string>();
        const receipts = snapshot.receipts.filter((row) => {
          if (received.has(row.commandId)) return false;
          received.add(row.commandId);
          return true;
        });
        state = { ...state, receipts, moreAvailable: snapshot.moreAvailable, connection: "current" };
      } else if (frame.caughtUp === undefined || resumeToken === null) {
        throw new Error("Dispatcher omitted the recent delivery issues snapshot");
      }
      resumeToken = frame.resumeToken;
      if (state.connection !== "current") state = { ...state, connection: "current" };
      if (state !== previous) publish(state);
    },
    onConnection: ({ phase }) => {
      if (phase === "reconnecting") {
        state = { ...state, connection: "stale" };
        publish(state);
      }
    },
    onError: (reason) => {
      if (outOfRange(reason)) {
        resumeToken = null;
        state = { ...state, coverageLost: true };
      }
    },
  });
}
