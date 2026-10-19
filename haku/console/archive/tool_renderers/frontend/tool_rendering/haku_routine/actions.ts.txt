// Action descriptions for the in-process haku_routine server's tools (see ../action_entry.ts).

import { type ActionEntry, fixed } from "../action_entry";

export const hakuRoutineActions: Record<string, ActionEntry> = {
  launch_routine: fixed("Haku: Start a new run"),
};
