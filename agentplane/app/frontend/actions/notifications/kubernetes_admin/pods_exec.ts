import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

import { kubernetesTarget } from "./target";

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
    text: request.description ?? "Action requires approval",
  };
}
