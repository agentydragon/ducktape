import { dispatchFromJson } from "./client";
import { HttpError } from "../transport/client";
import { sessionDispatcherClient } from "../transport/session_dispatcher_client";
import { WatchStreamError } from "../transport/watch_stream";
import { canonicalDispatchCommandIds, type LocalCommands } from "./local_commands";

/** `DispatchFilter.command_ids`' bound; the commands past it go unwatched. */
const MAX_WATCHED_COMMANDS = 1000;

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
  return sessionDispatcherClient.watchDispatches(nextQuery, {
    onOpen: () => {
      held = new Set();
    },
    onFrame: (result) => {
      try {
        if (typeof result !== "object" || result === null || Array.isArray(result)) {
          throw new Error("Dispatcher returned an invalid watch frame");
        }
        const frame = result as { dispatch?: unknown; caughtUp?: unknown; resumeToken?: unknown };
        if (typeof frame.resumeToken !== "string" || frame.resumeToken === "") {
          throw new Error("Dispatcher watch frame omitted its resume token");
        }
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
      if (connection.phase === "reconnecting") {
        store.setDispatchWatchError(connection.lastError ?? "The Dispatcher closed the stream.");
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
}
