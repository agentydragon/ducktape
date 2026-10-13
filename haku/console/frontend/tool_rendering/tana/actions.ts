// Action descriptions for tana's tools (see ../action_entry.ts).

import { type ActionEntry, fixed, fromArgs } from "../action_entry";
import { zSetFieldOptionArgs } from "./schemas";

export const tanaActions: Record<string, ActionEntry> = {
  import_tana_paste: fixed("Tana: Import content"),
  trash_node: fixed("Tana: Trash node", true),
  edit_node: fixed("Tana: Edit node"),
  move_node: fixed("Tana: Move node"),
  set_field_option: fromArgs(zSetFieldOptionArgs, (a) => ({
    text: `Tana: ${a.mode === "append" ? "Append" : "Set"} field option`,
  })),
};
