import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { resourceApplyArguments } from "../../schemas/kubernetes_admin/arguments";
import { kubernetesResourceSummary } from "../../schemas/kubernetes_admin/manifest";

export function resourcesCreateOrUpdateNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = resourceApplyArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: kubernetesResourceSummary(parsed.data.resource) ?? "Apply Kubernetes resource",
    text: request.description ?? "Action requires approval",
  };
}
