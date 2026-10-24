import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

import { podsLogOptions } from "../../schemas/kubernetes_admin/pods_log";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

const podsLogNotificationArguments = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  container: z.string().min(1).optional(),
  previous: z.boolean().optional(),
  tail: z.int().optional(),
});

export function podsLogNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = podsLogNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: [
      `View logs for ${kubernetesTarget("Pod", parsed.data.name, parsed.data.namespace)}`,
      ...podsLogOptions(parsed.data),
    ].join(", "),
    text: request.description ?? "Action requires approval",
  };
}
