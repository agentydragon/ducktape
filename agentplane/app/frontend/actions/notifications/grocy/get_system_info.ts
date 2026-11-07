import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import type { ActionNotificationParts } from "../types";

export function grocySystemInfoNotification(request: ActionRequest): ActionNotificationParts {
  return {
    actionTitle: "Show Grocy system information",
    text: request.description ?? "Action requires approval",
  };
}
