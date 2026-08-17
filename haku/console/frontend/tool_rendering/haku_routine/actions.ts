// Card and notification action descriptions for the in-process haku_routine server's tools. React-free so `../../sw.ts`
// can bundle them (see ../action_entry.ts).

import { type ActionEntry, fixed } from "../action_entry";

export const hakuRoutineActions: Record<string, ActionEntry> = {
  launch_routine: fixed("Haku: Start a new run"),
};
