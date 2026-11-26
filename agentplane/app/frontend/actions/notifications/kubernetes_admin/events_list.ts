import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { eventsListArguments } from "../../schemas/kubernetes_admin/arguments";

export function eventsListNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = eventsListArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;
  const { namespace } = parsed.data;

  return {
    actionTitle: `List events${namespace === undefined ? "" : ` in namespace ${namespace}`}`,
    text: request.description ?? "Action requires approval",
  };
}
