// Notification/card action descriptions for kubectl-passthrough-mcp's tools — the one-line summary the
// approvals card shows and a push notification is titled with. Beside the widgets they
// describe, and React-free so `../../sw.ts` can bundle them (see ../action_entry.ts).

import { fixed, fromArgs } from "../action_entry";
import type { ActionEntry } from "../action_entry";
import { zResourcesDeleteArgs } from "./schemas";

export const kubectlActions: Record<string, ActionEntry> = {
  resources_create_or_update: fixed("kubectl: Apply resource"),
  resources_delete: fromArgs(zResourcesDeleteArgs, (a) => ({
    text: `kubectl: Delete ${a.kind}`,
    destructive: true,
  })),
  pods_delete: fixed("kubectl: Delete Pod", true),
  pods_log: fixed("kubectl: View Pod logs"),
};
