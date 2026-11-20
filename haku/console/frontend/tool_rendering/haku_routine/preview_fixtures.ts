// Focused samples for the historical routine card and its stored result.
import type { RegisteredToolPreviewFixture } from "../index";

export const PREVIEW_FIXTURES: (RegisteredToolPreviewFixture & { title: string })[] = [
  {
    title: "Review inbox for replies",
    serverId: "haku_routine",
    toolName: "launch_routine",
    args: { text: "Scan Gmail for anything needing a reply, draft responses, and flag time-sensitive items." },
  },
  {
    title: "Launch the default routine",
    serverId: "haku_routine",
    toolName: "launch_routine",
    args: {},
  },
  {
    title: "Review the incoming queue",
    serverId: "haku_routine",
    toolName: "launch_routine",
    args: { text: "Summarize new requests." },
    result: { session_url: "https://claude.ai/code/session_example" },
  },
];
