// `tana-rw` preview screenshot entry — esbuild bundles this into the `:previews` IIFE. Holds the
// fixtures plus the mount call; the Tana-only fetch stub is imported before the widget graph
// before the registry/widget graph reaches client.ts. `satisfies RegisteredToolPreviewFixture` ties
// each (serverId, toolName, args) to the registry's real Zod schemas, so a stale id or argument
// is a type error.
import "./preview_mock.ts";

import { mountPreviewCards } from "../screenshot/mount.tsx";

import type { RegisteredToolPreviewFixture } from "../index.tsx";

const PREVIEW_FIXTURES = [
  {
    title: "Add planning review tasks to Tana",
    serverId: "tana-rw",
    toolName: "import_tana_paste",
    args: {
      parentNodeId: "inbox",
      content: "- Prepare planning review\n  - Gather Q3 notes\n  - Draft agenda\n  - Confirm attendees",
    },
  },
  {
    title: "Open today's calendar node",
    serverId: "tana-rw",
    toolName: "get_or_create_calendar_node",
    args: { workspaceId: "workspace", granularity: "day", date: "2026-07-11" },
  },
  {
    title: "Trash the obsolete task",
    serverId: "tana-rw",
    toolName: "trash_node",
    args: { nodeId: "task" },
  },
  {
    title: "Rename the quarterly task",
    serverId: "tana-rw",
    toolName: "edit_node",
    args: { nodeId: "task", name: { old_string: "Quarterly", new_string: "Q3", replace_all: false } },
  },
  {
    title: "Move the task into its project",
    serverId: "tana-rw",
    toolName: "move_node",
    args: {
      nodeId: "task",
      targetNodeId: "project",
      sourceParentId: "old-parent",
      position: "end",
      keepSourceReference: true,
    },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
