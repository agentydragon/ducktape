import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

const podsInNamespaceNotificationArguments = z.strictObject({
  namespace: z.string().min(1),
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
});

export function podsInNamespaceNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = podsInNamespaceNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `List pods in namespace ${parsed.data.namespace}`,
    text: request.description ?? "Action requires approval",
  };
}
