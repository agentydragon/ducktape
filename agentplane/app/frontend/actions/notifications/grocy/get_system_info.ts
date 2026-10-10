import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

export function grocySystemInfoNotification(request: ActionRequestView): ActionNotificationParts {
  return {
    actionTitle: "Show Grocy system information",
    text: request.description ?? "Action requires approval",
  };
}
