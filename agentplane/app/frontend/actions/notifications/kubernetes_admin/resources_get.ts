import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

import { kubernetesTarget } from "./target";

const resourcesGetNotificationArguments = z.strictObject({
  apiVersion: z.string().min(1),
  kind: z.string().min(1),
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
});

export function resourcesGetNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = resourcesGetNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `Get ${kubernetesTarget(parsed.data.kind, parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
