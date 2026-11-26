import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { podsGetArguments } from "../../schemas/kubernetes_admin/arguments";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

export function podsGetNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = podsGetArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `Get ${kubernetesTarget("Pod", parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
