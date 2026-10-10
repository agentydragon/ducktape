import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

export function tanaCalendarNodeNotification(request: ActionRequestView): ActionNotificationParts {
  return {
    actionTitle: "Get or create calendar node",
    text: request.description ?? "Action requires approval",
  };
}
