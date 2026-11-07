import { fromJson, toJson } from "@bufbuild/protobuf";
import { TimestampSchema } from "@bufbuild/protobuf/wkt";

import { GetSystemStatusResponseSchema, SystemComponentState } from "../app_service_pb";
import { createAppServiceClient } from "./app_service_client";

export type SystemComponentStatus = {
  name: string;
  state: "ok" | "down";
  detail: string | null;
  checked_at: string;
};

const appServiceClient = createAppServiceClient("/upstream/app/v1", "Invalid system status response.");

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

export async function getSystemStatus(): Promise<SystemComponentStatus[]> {
  const response = fromJson(GetSystemStatusResponseSchema, await appServiceClient.get("/system/status"));
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
