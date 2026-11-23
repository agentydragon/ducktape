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

type Columns = Record<string, { type: string; not_null: boolean; pk_index?: number }>;

/**
 * Electric's `electric-schema` header for each relation the Thread store syncs. Primary-key columns come
 * first, in key order.
 */
export const ELECTRIC_SCHEMAS: Record<string, Columns> = {
  thread_entity: {
    thread_id: { type: "uuid", not_null: true, pk_index: 0 },
    projection_epoch: { type: "text", not_null: true, pk_index: 1 },
    entity_kind: { type: "text", not_null: true, pk_index: 2 },
    entity_id: { type: "text", not_null: true, pk_index: 3 },
    entity_index: { type: "int8", not_null: true },
    cursor: { type: "int8", not_null: true },
    revision_cursor: { type: "int8", not_null: true },
    pending: { type: "bool", not_null: true },
    state: { type: "jsonb", not_null: true },
  },
  thread_payload_chunk: {
    thread_id: { type: "uuid", not_null: true, pk_index: 0 },
    projection_epoch: { type: "text", not_null: true, pk_index: 1 },
    owner_cursor: { type: "int8", not_null: true, pk_index: 2 },
    owner_id: { type: "text", not_null: true, pk_index: 3 },
    field: { type: "text", not_null: true, pk_index: 4 },
    generation: { type: "int8", not_null: true, pk_index: 5 },
    chunk_index: { type: "int8", not_null: true, pk_index: 6 },
    text: { type: "json", not_null: true },
  },
};

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
