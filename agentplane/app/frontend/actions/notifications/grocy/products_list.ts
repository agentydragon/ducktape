import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

export function grocyProductsListNotification(request: ActionRequestView): ActionNotificationParts {
  return {
    actionTitle: "List Grocy products",
    text: request.description ?? "Action requires approval",
  };
}
