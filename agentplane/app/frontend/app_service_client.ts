import type { JsonValue } from "@bufbuild/protobuf";

import { fetchWithLogin, HttpError } from "./client";

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  if (typeof value !== "object") return false;
  return Object.values(value).every(isJsonValue);
}

export function createAppServiceClient(basePath: string, invalidResponseMessage: string) {
  return {
    async get(path: string): Promise<JsonValue> {
      const response = await fetchWithLogin(`${basePath}${path}`, { headers: { Accept: "application/json" } });
      const text = await response.text();
      let payload: unknown;
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
      if (!response.ok) throw new HttpError(response, payload);
      if (!isJsonValue(payload)) throw new Error(invalidResponseMessage);
      return payload;
    },
  };
}
