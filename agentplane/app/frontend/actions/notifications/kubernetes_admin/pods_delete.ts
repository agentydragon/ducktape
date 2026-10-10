import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";
import { z } from "zod";

import { kubernetesTarget } from "./target";

const podsDeleteNotificationArguments = z.object({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
});

export function podsDeleteNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = podsDeleteNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `⚠ Delete ${kubernetesTarget("Pod", parsed.data.name, parsed.data.namespace)}`,
    text: request.description ?? "Action requires approval",
  };
}
