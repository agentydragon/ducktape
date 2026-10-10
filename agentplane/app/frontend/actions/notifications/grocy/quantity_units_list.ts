import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

export function grocyQuantityUnitsListNotification(request: ActionRequestView): ActionNotificationParts {
  return {
    actionTitle: "List quantity units",
    text: request.description ?? "Action requires approval",
  };
}
