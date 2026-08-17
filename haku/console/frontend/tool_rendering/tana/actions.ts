// Card and notification action descriptions for tana-rw's tools. React-free so `../../sw.ts`
// can bundle them (see ../action_entry.ts).

import { type ActionEntry, fixed, fromArgs } from "../action_entry";
import { zSetFieldOptionArgs } from "./schemas";

export const tanaActions: Record<string, ActionEntry> = {
  import_tana_paste: fixed("Tana: Import content"),
  get_or_create_calendar_node: fixed("Tana: Get or create calendar node"),
  trash_node: fixed("Tana: Trash node", true),
  edit_node: fixed("Tana: Edit node"),
  move_node: fixed("Tana: Move node"),
  set_field_option: fromArgs(zSetFieldOptionArgs, (a) => ({
    text: `Tana: ${a.mode === "append" ? "Append" : "Set"} field option`,
  })),
};
