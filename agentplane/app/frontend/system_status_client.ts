import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";

import { GetSystemStatusResponseSchema, SystemComponentState } from "../app_service_pb";
import { fetchWithLogin, HttpError } from "./client";

export type SystemComponentStatus = {
  name: string;
  state: "ok" | "down";
  detail: string | null;
  checked_at: string;
};

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  if (typeof value !== "object") return false;
  return Object.values(value).every(isJsonValue);
}

function stateName(state: SystemComponentState): SystemComponentStatus["state"] {
  switch (state) {
    case SystemComponentState.OK:
      return "ok";
    case SystemComponentState.DOWN:
      return "down";
    default:
      throw new Error(`App status returned an unknown system component state (${state}).`);
  }
}

async function readSystemStatus(): Promise<JsonValue> {
  const response = await fetchWithLogin("/upstream/app/v1/system/status", {
    headers: { Accept: "application/json" },
  });
  const text = await response.text();
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = text;
  }
  if (!response.ok) throw new HttpError(response, payload);
  if (!isJsonValue(payload)) throw new Error("Invalid system status response.");
  return payload;
}

export async function getSystemStatus(): Promise<SystemComponentStatus[]> {
  const response = fromJson(GetSystemStatusResponseSchema, await readSystemStatus());
  return response.components.map((component) => {
    if (component.checkedAt === undefined) throw new Error("System status omitted a component check time.");
    return {
      name: component.name,
      state: stateName(component.state),
      detail: component.detail ?? null,
      checked_at: toJson(TimestampSchema, component.checkedAt) as string,
    };
  });
}
