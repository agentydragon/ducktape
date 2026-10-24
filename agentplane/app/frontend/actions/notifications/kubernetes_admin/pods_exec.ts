import { z } from "zod";

import type { ActionRequestView } from "../../types";
import { argvText } from "../../../argv_text";
import { commandNotificationText } from "../command";
import type { ActionNotificationParts } from "../types";

import { podsExecNotes } from "../../schemas/kubernetes_admin/pods_exec";
import { kubernetesTarget } from "../../schemas/kubernetes_admin/target";

const podsExecNotificationArguments = z.strictObject({
  name: z.string().min(1),
  namespace: z.string().min(1).optional(),
  container: z.string().min(1).optional(),
  command: z.array(z.string()),
});

export function podsExecNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = podsExecNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;

  return {
    actionTitle: `Run command in ${kubernetesTarget("Pod", parsed.data.name, parsed.data.namespace)}`,
    text: commandNotificationText(argvText(parsed.data.command), podsExecNotes(parsed.data)),
  };
}
