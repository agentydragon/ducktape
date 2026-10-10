import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

export function resourcesCreateOrUpdateNotification(request: ActionRequestView): ActionNotificationParts {
  return {
    actionTitle: "Apply Kubernetes resource",
    text: request.description ?? "Action requires approval",
  };
}
