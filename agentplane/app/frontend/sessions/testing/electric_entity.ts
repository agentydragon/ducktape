import { toJson, type JsonValue } from "@bufbuild/protobuf";

import {
  ThreadCommandOutcome,
  ThreadCommandSchema,
  ThreadConfirmedInputSchema,
  ThreadItemSchema,
  ThreadLifecycleSchema,
  ThreadViewStateSchema,
  type ThreadEntity,
} from "../../../../history_service/api/history/history_service_pb";

/** Encode a protobuf variant as the indexed columns of an Electric entity row. */
export function electricEntityVariant(entity: ThreadEntity["entity"]): {
  kind: string;
  pending: boolean;
  state: JsonValue;
} {
  switch (entity.case) {
    case "viewState":
      return { kind: "view_state", pending: false, state: toJson(ThreadViewStateSchema, entity.value) };
    case "item":
      return { kind: "item", pending: false, state: toJson(ThreadItemSchema, entity.value) };
    case "confirmedInput":
      return { kind: "confirmed_input", pending: false, state: toJson(ThreadConfirmedInputSchema, entity.value) };
    case "lifecycle":
      return { kind: "lifecycle", pending: false, state: toJson(ThreadLifecycleSchema, entity.value) };
    case "command":
      return {
        kind: "command",
        pending: entity.value.outcome === ThreadCommandOutcome.PENDING,
        state: toJson(ThreadCommandSchema, entity.value),
      };
    case undefined:
      throw new Error("Fixture entity has no variant.");
  }
}
