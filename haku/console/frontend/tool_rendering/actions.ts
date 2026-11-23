/** Danger cues for historical tool cards whose titles come from the stored record. */
export function isDestructiveToolCall(serverId: string, toolName: string): boolean {
  return (
    (serverId === "tana" && toolName === "trash_node") ||
    (serverId === "google_calendar" && toolName === "delete_event") ||
    (serverId === "grocy-sf" && toolName === "shopping_list_items_remove")
  );
}
