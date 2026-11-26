import { actionArguments } from "../../presentation";

import type { ActionRequest } from "../../../../../action_service/api/contracts/action_service_pb";
import { argvText } from "../../../ui/argv_text";
import { commandNotificationText } from "../command";
import type { ActionNotificationParts } from "../types";

import { podsExecNotes } from "../../schemas/kubernetes_admin/pods_exec";
import { podsExecArguments } from "../../schemas/kubernetes_admin/arguments";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

export function podsExecNotification(request: ActionRequest): ActionNotificationParts | null {
  const parsed = podsExecArguments.safeParse(actionArguments(request));
  if (!parsed.success) return null;

  return {
    actionTitle: `Run command in ${kubernetesTarget("Pod", parsed.data.name, parsed.data.namespace)}`,
    text: commandNotificationText(argvText(parsed.data.command), podsExecNotes(parsed.data)),
  };
}
