import { create } from "@bufbuild/protobuf";
import {
  EntityKeySchema,
  type EntityKey,
  ThreadEntityKind,
  type ThreadEntity,
  type ThreadPayloadReference,
} from "../../../history_service/history_service_pb";

export function threadEntityKind(entity: ThreadEntity): ThreadEntityKind {
  switch (entity.entity.case) {
    case "viewState":
      return ThreadEntityKind.VIEW_STATE;
    case "item":
      return ThreadEntityKind.ITEM;
    case "confirmedInput":
      return ThreadEntityKind.CONFIRMED_INPUT;
    case "lifecycle":
      return ThreadEntityKind.LIFECYCLE;
    case "command":
      return ThreadEntityKind.COMMAND;
    case undefined:
      throw new Error("Thread entity has no oneof variant.");
  }
}

export function threadEntityKey(entity: ThreadEntity): EntityKey {
  return create(EntityKeySchema, { entityKind: threadEntityKind(entity), entityId: entity.entityId });
}

export function threadEntityTurnId(entity: ThreadEntity): string | null {
  switch (entity.entity.case) {
    case "item":
      return entity.entity.value.turnId ?? null;
    case "confirmedInput":
      return entity.entity.value.turnId ?? null;
    default:
      return null;
  }
}

export function threadEntityPayloadReferences(entity: ThreadEntity): ThreadPayloadReference[] {
  switch (entity.entity.case) {
    case "item":
      return [entity.entity.value.textRef, entity.entity.value.argumentsRef, entity.entity.value.outputRef].filter(
        (reference): reference is ThreadPayloadReference => reference !== undefined
      );
    case "confirmedInput":
    case "command":
      return entity.entity.value.inputRef === undefined ? [] : [entity.entity.value.inputRef];
    default:
      return [];
  }
}
