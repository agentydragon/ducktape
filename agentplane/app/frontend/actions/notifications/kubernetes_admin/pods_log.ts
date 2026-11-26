import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { podsLogOptions } from "../../schemas/kubernetes_admin/pods_log";
import { podsLogArguments } from "../../schemas/kubernetes_admin/arguments";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

export function podsLogNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = podsLogArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: [
      `View logs for ${kubernetesTarget("Pod", parsed.data.name, parsed.data.namespace)}`,
      ...podsLogOptions(parsed.data),
    ].join(", "),
    text: request.description ?? "Action requires approval",
  };
}
