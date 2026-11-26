import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { resourcesDeleteArguments } from "../../schemas/kubernetes_admin/arguments";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

export function resourcesDeleteNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = resourcesDeleteArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `⚠ Delete ${kubernetesTarget(parsed.data.kind, parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
