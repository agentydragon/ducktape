import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import { DeliveryState, DispatchSchema, WatchDispatchesResponseSchema } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";
import { dispatchFromJson, isUnknownDispatchState } from "./client";
import { followWatch } from "../watch_stream";
import type { LocalCommands } from "./local_commands";

/** DispatchFilter.command_ids bound; commands past it go unwatched. */
const MAX_WATCHED_COMMANDS = 1000;

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  return typeof value === "object" && Object.values(value).every(isJsonValue);
}

function decodeDispatchFrame(value: unknown): { kind: "dispatch"; dispatch: ReturnType<typeof dispatchFromJson> } | { kind: "caughtUp" } {
  if (!isJsonValue(value) || typeof value !== "object" || value === null || Array.isArray(value))
    throw new TypeError("Session Dispatcher watch result must be a JSON object");
  const hasDispatch = Object.prototype.hasOwnProperty.call(value, "dispatch");
  const hasCaughtUp = Object.prototype.hasOwnProperty.call(value, "caughtUp");
  if (hasDispatch === hasCaughtUp)
    throw new TypeError("Session Dispatcher watch result must contain exactly one frame kind");

  const response = fromJson(WatchDispatchesResponseSchema, value);
  if (response.frame.case === "dispatch") {
    const proto = response.frame.value;
    // Proto3 permits omitted scalars, but the Dispatcher always writes identity and state.
    // Unknown enum names are rejected; future numeric values remain explicit unknown variants.
    if (proto.commandId === "" || proto.sessionId === "" || proto.state === DeliveryState.UNSPECIFIED)
      throw new TypeError("Session Dispatcher watch returned a Dispatch without its command, Session or state");
    const dispatch = dispatchFromJson(toJson(DispatchSchema, proto));
    if (isUnknownDispatchState(dispatch.state)) {
      return {
        kind: "dispatch",
        dispatch: { ...dispatch, reason: dispatch.reason || "Unrecognized delivery state " + dispatch.state.unknown },
      };
    }
    return { kind: "dispatch", dispatch };
  }
  if (response.frame.case === "caughtUp") return { kind: "caughtUp" };
  throw new TypeError("Session Dispatcher watch result is missing its frame");
}

/** Keeps store current with Dispatcher rows for commandIds of one Session through the WatchDispatches relay. */
export function watchDispatches(store: LocalCommands, sessionId: string, commandIds: readonly string[]): () => void {
  const asked = commandIds.slice(0, MAX_WATCHED_COMMANDS);
  const query = new URLSearchParams({ "filter.sessionId": sessionId });
  for (const id of asked) query.append("filter.commandIds", id);
  // Each connection replays the Dispatcher's rows from the start.
  let held = new Set<string>();
  return followWatch("/upstream/dispatcher/v1/dispatches:watch?" + query, {
    onOpen: () => {
      held = new Set();
    },
    onFrame: (result) => {
      const frame = decodeDispatchFrame(result);
      if (frame.kind === "dispatch") {
        held.add(frame.dispatch.commandId);
        store.updateDispatch(frame.dispatch.commandId, frame.dispatch);
      } else {
        // The Dispatcher deletes a Sandbox's rows with it.
        for (const commandId of asked.filter((id) => !held.has(id)))
          store.updateDispatch(
            commandId,
            dispatchFromJson({
              commandId,
              sessionId,
              state: "DELIVERY_STATE_UNKNOWN",
              reason: "sandbox_deleted",
            })
          );
      }
    },
    onConnection: (connection) => {
      if (connection.phase === "reconnecting" && connection.lastError !== null)
        console.warn("Session Dispatcher watch is reconnecting after a read error", connection.lastError);
    },
  });
}
