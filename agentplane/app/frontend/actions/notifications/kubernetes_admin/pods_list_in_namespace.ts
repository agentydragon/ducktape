import type { ActionRequestView } from "../../types";
import { zPodsInNamespaceArguments } from "../../schemas/kubernetes_admin/pods_list_in_namespace";
import type { ActionNotificationParts } from "../types";

export function podsInNamespaceNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = zPodsInNamespaceArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `List pods in namespace ${parsed.data.namespace}`,
    text: request.description ?? "Action requires approval",
  };
}
