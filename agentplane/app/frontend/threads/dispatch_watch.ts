import { dispatchReceipt } from "./client";
import { followWatch } from "../watch_stream";
import type { LocalCommands } from "./local_commands";

/** `DispatchFilter.command_ids`' bound; the commands past it go unwatched. */
const MAX_WATCHED_COMMANDS = 1000;

/** Keeps `store` current with the Dispatcher's rows for `commandIds` of one Session, through the app's
 * relay of `WatchDispatches`. Returns the function that stops it. */
export function watchDispatches(store: LocalCommands, sessionId: string, commandIds: readonly string[]): () => void {
  const asked = commandIds.slice(0, MAX_WATCHED_COMMANDS);
  const query = new URLSearchParams({ "filter.sessionId": sessionId });
  for (const id of asked) query.append("filter.commandIds", id);
  // The rows this connection has sent, since each replays the Dispatcher's rows from the start.
  let held = new Set<string>();
  return followWatch(`/upstream/dispatcher/v1/dispatches:watch?${query}`, {
    onOpen: () => {
      held = new Set();
    },
    onFrame: (result) => {
      try {
        const frame = result as { dispatch?: unknown; caughtUp?: unknown };
        if (frame.dispatch !== undefined) {
          const receipt = dispatchReceipt(frame.dispatch);
          held.add(receipt.commandId);
          store.updateDispatch(receipt.commandId, receipt);
        } else if (frame.caughtUp !== undefined) {
          // The Dispatcher deletes a Sandbox's rows with it.
          for (const commandId of asked.filter((id) => !held.has(id)))
            store.updateDispatch(commandId, {
              commandId,
              sessionId,
              state: "DELIVERY_STATE_UNKNOWN",
              reason: "sandbox_deleted",
              admission: null,
            });
        }
      } catch (reason) {
        console.warn("a dispatch frame could not be applied", reason);
      }
    },
    onConnection: () => undefined,
  });
}
