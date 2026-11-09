import { fromJsonString } from "@bufbuild/protobuf";
import type { Timestamp } from "@bufbuild/protobuf/wkt";

import { GetSystemStatusResponseSchema, SystemComponentState, type SystemComponentStatus } from "../app_service_pb";
import { createAppServiceClient } from "./app_service_client";

/** A component status whose state is known and whose check time is present. */
export type CheckedComponentStatus = SystemComponentStatus & { checkedAt: Timestamp };

const appServiceClient = createAppServiceClient("/upstream/app/v1");

export async function getSystemStatus(): Promise<CheckedComponentStatus[]> {
  const response = fromJsonString(GetSystemStatusResponseSchema, await appServiceClient.getText("/system/status"));
  return response.components.map((component) => {
    if (component.state !== SystemComponentState.OK && component.state !== SystemComponentState.DOWN)
      throw new Error(`App status returned an unknown system component state (${component.state}).`);
    const checkedAt = component.checkedAt;
    if (checkedAt === undefined) throw new Error("System status omitted a component check time.");
    return { ...component, checkedAt };
  });
}
