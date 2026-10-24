import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

const resourcesListNotificationArguments = z.strictObject({
  apiVersion: z.string().min(1),
  kind: z.string().min(1),
  namespace: z.string().min(1).optional(),
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
});

export function resourcesListNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = resourcesListNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const { kind, namespace } = parsed.data;

  return {
    actionTitle: `List ${kind} resources${namespace === undefined ? "" : ` in namespace ${namespace}`}`,
    text: request.description ?? "Action requires approval",
  };
}
