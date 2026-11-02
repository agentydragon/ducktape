import { create, toJson, type JsonValue, type MessageInitShape } from "@bufbuild/protobuf";
import { timestampFromDate, type Timestamp } from "@bufbuild/protobuf/wkt";
import { SandboxSchema, type Sandbox } from "../../../proto/sandboxes_pb";

/** Sandbox fixture values use the generated message's fields, not the app's legacy view. */
export type SandboxFixture = Exclude<MessageInitShape<typeof SandboxSchema>, Sandbox> &
  Pick<Sandbox, "name" | "namespace" | "uid">;

export function sandboxProto(value: SandboxFixture): JsonValue {
  return toJson(SandboxSchema, create(SandboxSchema, value));
}

export function sandboxTimestamp(value: string): Timestamp {
  return timestampFromDate(new Date(value));
}
