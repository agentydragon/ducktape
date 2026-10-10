// `tana` preview fixtures: the only list of this server's preview cards. The `:previews` target
// mounts them (`preview_harness.tsx`) and derives its screenshot scenarios from them
// (`screenshot/emit_scenarios.mjs`). `RegisteredToolPreviewFixture` ties each (serverId, toolName,
// args, result?) to the registry's real Zod schemas, so a stale id, argument, or result shape is a
// type error.
import type { RegisteredToolPreviewFixture } from "../index";

export const PREVIEW_FIXTURES: (RegisteredToolPreviewFixture & { title: string })[] = [
  {
    title: "Add planning review tasks to Tana",
    serverId: "tana",
    toolName: "import_tana_paste",
    args: {
      parentNodeId: "inbox",
      content: "- Prepare planning review\n  - Gather Q3 notes\n  - Draft agenda\n  - Confirm attendees",
    },
  },
  {
    title: "Trash the obsolete task",
    serverId: "tana",
    toolName: "trash_node",
    args: { nodeId: "task" },
  },
  {
    title: "Rename the quarterly task",
    serverId: "tana",
    toolName: "edit_node",
    args: { nodeId: "task", name: { old_string: "Quarterly", new_string: "Q3", replace_all: false } },
  },
  {
    title: "Move the task into its project",
    serverId: "tana",
    toolName: "move_node",
    args: {
      nodeId: "task",
      targetNodeId: "project",
      sourceParentId: "old-parent",
      position: "end",
      keepSourceReference: true,
    },
  },
];
