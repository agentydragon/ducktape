import { z } from "zod";
import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import type { ActionNotificationParts } from "../types";

const resourcesListNotificationArguments = z.strictObject({
  apiVersion: z.string().min(1),
  kind: z.string().min(1),
  namespace: z.string().min(1).optional(),
  fieldSelector: z.string().min(1).optional(),
  labelSelector: z.string().min(1).optional(),
});

export function resourcesListNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = resourcesListNotificationArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;
  const { kind, namespace } = parsed.data;

  return {
    actionTitle: `List ${kind} resources${namespace === undefined ? "" : ` in namespace ${namespace}`}`,
    text: request.description ?? "Action requires approval",
  };
}
