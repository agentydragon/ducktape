import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import type { ActionNotificationParts } from "../types";

export function grocyQuantityUnitsListNotification(request: ActionRequest): ActionNotificationParts {
  return {
    actionTitle: "List quantity units",
    text: request.description ?? "Action requires approval",
  };
}
