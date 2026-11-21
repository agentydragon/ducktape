import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { zPodsInNamespaceArguments } from "../../schemas/kubernetes_admin/pods_list_in_namespace";

export function podsInNamespaceNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = zPodsInNamespaceArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `List pods in namespace ${parsed.data.namespace}`,
    text: request.description ?? "Action requires approval",
  };
}
