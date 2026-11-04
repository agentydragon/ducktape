import type { JsonValue } from "@bufbuild/protobuf";

/** Validates a JSON value before handing it to Buf's generated ProtoJSON reader. */
export function protoJson(value: unknown): JsonValue {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (Array.isArray(value)) return value.map(protoJson);
  if (typeof value === "object") {
    const result: { [key: string]: JsonValue } = {};
    for (const [key, child] of Object.entries(value)) result[key] = protoJson(child);
    return result;
  }
  throw new Error("Watch stream returned a non-JSON value.");
}
