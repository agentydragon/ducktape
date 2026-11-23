import type { ActionRequest } from "../../../../action_service/api/contracts/action_service_pb";
import { actionArguments, actionIdentity } from "../presentation";
import { modifyThreadLabelsArguments, moveNodeArguments } from "../schemas/integration_previews";
import type { ActionNotificationParts } from "./types";

export function notificationPreviewKind(request: ActionRequest): "gmail" | "tana" | undefined {
  const action = actionIdentity(request);
  const args = actionArguments(request);
  if (
    action.group === "gmail" &&
    action.name === "threads_modify_labels" &&
    modifyThreadLabelsArguments.safeParse(args).success
  ) {
    return "gmail";
  }
  if (action.group === "tana" && action.name === "move_node" && moveNodeArguments.safeParse(args).success) {
    return "tana";
  }
  return undefined;
}

export function gmailThreadLabelsNotification(
  request: ActionRequest,
  names?: ReadonlyMap<string, string>
): ActionNotificationParts {
  const parsed = modifyThreadLabelsArguments.safeParse(actionArguments(request));
  const fallback = request.description ?? "Action requires approval";
  if (!parsed.success) return { actionTitle: "Change Gmail thread labels", text: fallback };

  const ids = [...new Set(parsed.data.thread_ids)];
  const subjects = ids
    .slice(0, 2)
    .map((id) => names?.get(id))
    .filter(Boolean);
  const remaining = ids.length - subjects.length;
  const count = remaining === 1 ? "1 thread" : `${remaining} threads`;
  const targets = subjects.length === 0 ? count : [...subjects, ...(remaining ? [`+${count}`] : [])].join(" · ");
  const text = request.description
    ? `${targets}
${request.description}`
    : targets;
  return { actionTitle: "Change Gmail thread labels", text };
}

export function tanaMoveNodeNotification(
  request: ActionRequest,
  names?: ReadonlyMap<string, string>
): ActionNotificationParts {
  const parsed = moveNodeArguments.safeParse(actionArguments(request));
  const node = parsed.success ? names?.get(parsed.data.nodeId) : undefined;
  const destination = parsed.success ? names?.get(parsed.data.targetNodeId) : undefined;
  return {
    actionTitle: "Move Tana node",
    text:
      node || destination
        ? `${node || "Node"} → ${destination || "destination"}`
        : (request.description ?? "Action requires approval"),
  };
}
