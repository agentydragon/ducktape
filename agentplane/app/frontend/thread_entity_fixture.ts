import type { ItemKind } from "../../protocol/event_pb";
import type { ThreadEntity } from "./thread_sync";

type ItemState = Extract<ThreadEntity["state"], { kind: number }>;

export function testEntity(
  cursor: number,
  entityKind: ThreadEntity["entityKind"],
  state: ThreadEntity["state"],
  overrides: Partial<ThreadEntity> = {}
): ThreadEntity {
  return {
    threadId: "test-thread",
    projectionEpoch: "test-epoch",
    entityKind,
    entityId: `test-entity-${cursor}`,
    entityIndex: String(cursor),
    cursor: String(cursor),
    revisionCursor: String(cursor),
    pending: false,
    turnId: "test-turn",
    state,
    textRef: null,
    argumentsRef: null,
    outputRef: null,
    inputRef: null,
    ...overrides,
  };
}

/** A completed item unless `state` says otherwise. */
export function testItem(
  cursor: number,
  kind: ItemKind,
  state: Partial<ItemState> = {},
  overrides: Partial<ThreadEntity> = {}
): ThreadEntity {
  return testEntity(
    cursor,
    "item",
    { kind, tool_name: "", completion: "text", tool_succeeded: null, ...state },
    overrides
  );
}
