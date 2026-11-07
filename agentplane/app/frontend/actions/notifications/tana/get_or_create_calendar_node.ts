import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import type { ActionNotificationParts } from "../types";

export function tanaCalendarNodeNotification(request: ActionRequest): ActionNotificationParts {
  return {
    actionTitle: "Get or create calendar node",
    text: request.description ?? "Action requires approval",
  };
}
