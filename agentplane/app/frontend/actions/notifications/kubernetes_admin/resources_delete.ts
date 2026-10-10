import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

import { kubernetesTarget } from "./target";

const resourcesDeleteNotificationArguments = z.strictObject({
  apiVersion: z.string().min(1),
  kind: z.string().min(1),
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  gracePeriodSeconds: z.int().nonnegative().optional(),
});

export function resourcesDeleteNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = resourcesDeleteNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `⚠ Delete ${kubernetesTarget(parsed.data.kind, parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
