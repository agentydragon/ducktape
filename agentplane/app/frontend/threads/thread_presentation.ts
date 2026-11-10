import { toJson } from "@bufbuild/protobuf";
import { TimestampSchema, type Timestamp } from "@bufbuild/protobuf/wkt";

/** ISO text used by time labels, derived from the generated timestamp at the UI boundary. */
export function timestampLabel(value: Timestamp | undefined): string | null {
  if (!value) return null;
  try {
    const json = toJson(TimestampSchema, value);
    return typeof json === "string" ? json : null;
  } catch {
    return null;
  }
}
