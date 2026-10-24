import { z } from "zod";

import type { ActionRequestView } from "../../types";
import type { ActionNotificationParts } from "../types";

const eventsListNotificationArguments = z.strictObject({
  namespace: z.string().min(1).optional(),
  fieldSelector: z.string().min(1).optional(),
});

export function eventsListNotification(request: ActionRequestView): ActionNotificationParts | null {
  const parsed = eventsListNotificationArguments.safeParse(request.arguments);
  if (!parsed.success) return null;
  const { namespace } = parsed.data;

  return {
    actionTitle: `List events${namespace === undefined ? "" : ` in namespace ${namespace}`}`,
    text: request.description ?? "Action requires approval",
  };
}
