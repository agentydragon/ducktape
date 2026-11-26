import { toJson } from "@bufbuild/protobuf";
import { type Timestamp, TimestampSchema } from "@bufbuild/protobuf/wkt";

/** Narrows a message field the service always sets; protobuf-es types every message field optional. */
export function required<T>(value: T | undefined, field: string): T {
  if (value === undefined) throw new Error(`The response omitted ${field}.`);
  return value;
}

/** A protobuf timestamp as its RFC 3339 JSON string. */
export function timestampText(value: Timestamp | undefined): string | undefined {
  if (value === undefined) return undefined;
  const json = toJson(TimestampSchema, value);
  if (typeof json !== "string") throw new Error("The response carried an invalid timestamp.");
  return json;
}
