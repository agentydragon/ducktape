import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import type { ActionNotificationParts } from "../types";

import { resourcesListArguments } from "../../schemas/kubernetes_admin/arguments";

export function resourcesListNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = resourcesListArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;
  const { kind, namespace } = parsed.data;

  return {
    actionTitle: `List ${kind} resources${namespace === undefined ? "" : ` in namespace ${namespace}`}`,
    text: request.description ?? "Action requires approval",
  };
}
