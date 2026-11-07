import { z } from "zod";
import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { kubernetesResourceSummary } from "../../schemas/kubernetes_admin/manifest";

const resourcesCreateOrUpdateNotificationArguments = z.strictObject({ resource: z.string().min(1) });

export function resourcesCreateOrUpdateNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = resourcesCreateOrUpdateNotificationArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: kubernetesResourceSummary(parsed.data.resource) ?? "Apply Kubernetes resource",
    text: request.description ?? "Action requires approval",
  };
}
