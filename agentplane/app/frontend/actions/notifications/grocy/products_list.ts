import type { ActionRequest } from "../../../../../action_service/action_service_pb";
import type { ActionNotificationParts } from "../types";

export function grocyProductsListNotification(request: ActionRequest): ActionNotificationParts {
  return {
    actionTitle: "List Grocy products",
    text: request.description ?? "Action requires approval",
  };
}
