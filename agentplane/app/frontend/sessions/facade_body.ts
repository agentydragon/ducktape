import type { JsonValue } from "@bufbuild/protobuf";

/** Serialize an HTTP facade's ProtoJSON body without the field supplied by its URL path. */
export function facadeBody(value: JsonValue, pathField: string): string {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Request did not serialize to a JSON object");
  }
  const body = { ...value };
  delete body[pathField];
  return JSON.stringify(body);
}
