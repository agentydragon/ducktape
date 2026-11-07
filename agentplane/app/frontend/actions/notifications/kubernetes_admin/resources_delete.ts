import { z } from "zod";
import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

const resourcesDeleteNotificationArguments = z.strictObject({
  apiVersion: z.string().min(1),
  kind: z.string().min(1),
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  gracePeriodSeconds: z.int().nonnegative().optional(),
});

export function resourcesDeleteNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = resourcesDeleteNotificationArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `⚠ Delete ${kubernetesTarget(parsed.data.kind, parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
