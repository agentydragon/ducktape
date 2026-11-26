import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { resourcesGetArguments } from "../../schemas/kubernetes_admin/arguments";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

export function resourcesGetNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = resourcesGetArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `Get ${kubernetesTarget(parsed.data.kind, parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
