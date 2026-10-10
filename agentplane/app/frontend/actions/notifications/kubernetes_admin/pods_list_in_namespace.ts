import type { ActionRequestView } from "../../types";
import { zPodsInNamespaceArguments } from "../../schemas/kubernetes_admin/pods_list_in_namespace";
import type { ActionNotificationContent } from "../types";

export function podsInNamespaceNotification(request: ActionRequestView): ActionNotificationContent | null {
  const parsed = zPodsInNamespaceArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    title: `${request.title} · List pods in namespace ${parsed.data.namespace}`,
    text: request.description ?? "Action requires approval",
  };
}
