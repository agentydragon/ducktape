import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { podsInNamespaceArguments } from "../../schemas/kubernetes_admin/arguments";

export function podsInNamespaceNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = podsInNamespaceArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `List pods in namespace ${parsed.data.namespace}`,
    text: request.description ?? "Action requires approval",
  };
}
