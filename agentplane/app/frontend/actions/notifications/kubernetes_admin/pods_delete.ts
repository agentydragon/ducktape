import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { podsDeleteArguments } from "../../schemas/kubernetes_admin/arguments";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

export function podsDeleteNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = podsDeleteArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `⚠ Delete ${kubernetesTarget("Pod", parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
