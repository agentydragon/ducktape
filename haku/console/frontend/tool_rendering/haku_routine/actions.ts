// Notification/card action descriptions for the in-process haku_routine server's tools — the one-line summary the
// approvals card shows and a push notification is titled with. Beside the widgets they
// describe, and React-free so `../../sw.ts` can bundle them (see ../action_entry.ts).

import { type ActionEntry, fixed } from "../action_entry";

export const hakuRoutineActions: Record<string, ActionEntry> = {
  launch_routine: fixed("Haku: Start a new run"),
};
