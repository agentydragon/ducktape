import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

const resourcesCreateOrUpdateNotificationArguments = z.strictObject({ resource: z.string().min(1) });

export function resourcesCreateOrUpdateNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = resourcesCreateOrUpdateNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: "Apply Kubernetes resource",
    text: request.description ?? "Action requires approval",
  };
}
