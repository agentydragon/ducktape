// `haku_routine` preview fixtures: the only list of this server's preview cards. The `:previews` target
// mounts them (`preview_harness.tsx`) and derives its screenshot scenarios from them
// (`screenshot/emit_scenarios.mjs`). `RegisteredToolPreviewFixture` ties each (serverId, toolName,
// args, result?) to the registry's real Zod schemas, so a stale id, argument, or result shape is a
// type error.
import type { RegisteredToolPreviewFixture } from "../index";

export const PREVIEW_FIXTURES: (RegisteredToolPreviewFixture & { title: string })[] = [
  {
    title: "Review inbox for replies",
    serverId: "haku_routine",
    toolName: "launch_routine",
    args: { text: "Scan Gmail for anything needing a reply, draft responses, and flag time-sensitive items." },
  },
];
