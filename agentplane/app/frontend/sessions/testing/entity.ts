import { create, type MessageInitShape } from "@bufbuild/protobuf";

import {
  ThreadEntitySchema,
  ThreadItemSchema,
  ThreadItemTextCompletionSchema,
  type ThreadItem,
  type ThreadEntity,
} from "../../../../history_service/api/history/history_service_pb";
import type { ItemKind } from "../../../../proto/event_pb";

export function testEntity(
  cursor: number,
  entity: MessageInitShape<typeof ThreadEntitySchema>["entity"],
  overrides: MessageInitShape<typeof ThreadEntitySchema> = {}
): ThreadEntity {
  return create(ThreadEntitySchema, {
    sessionId: "test-thread",
    projectionEpoch: "test-epoch",
    entityId: `test-entity-${cursor}`,
    entityIndex: BigInt(cursor),
    cursor: BigInt(cursor),
    revisionCursor: BigInt(cursor),
    entity,
    ...overrides,
  });
}

/** A completed item unless `item` says otherwise. */
export function testItem(
  cursor: number,
  kind: ItemKind,
  item: MessageInitShape<typeof ThreadItemSchema> = {},
  references: Pick<Partial<ThreadItem>, "textRef" | "argumentsRef" | "outputRef"> = {}
): ThreadEntity {
  const value = create(ThreadItemSchema, item);
  value.kind = kind;
  value.turnId ??= "test-turn";
  if (item.completion === undefined) {
    value.completion = { case: "text", value: create(ThreadItemTextCompletionSchema) };
  }
  Object.assign(value, references);
  return testEntity(cursor, { case: "item", value });
}
