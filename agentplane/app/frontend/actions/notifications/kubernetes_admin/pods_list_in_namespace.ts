import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

const podsInNamespaceNotificationArguments = z.object({ namespace: z.string().min(1) });

export function podsInNamespaceNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = podsInNamespaceNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `List pods in namespace ${parsed.data.namespace}`,
    text: request.description ?? "Action requires approval",
  };
}
