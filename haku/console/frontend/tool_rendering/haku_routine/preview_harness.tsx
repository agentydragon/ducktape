// `haku_routine` preview screenshot entry — esbuild bundles this into the `:previews` IIFE.
// Holds the fixtures plus the mount call; `mount` is imported FIRST so its fetch stub (mock.ts)
// is installed before the registry/widget graph reaches client.ts. `satisfies
// RegisteredToolPreviewFixture` ties each (serverId, toolName, args) to the registry's real Zod
// schemas, so a stale id or argument is a type error.
import { mountPreviewCards } from "../screenshot/mount.tsx";

import type { RegisteredToolPreviewFixture } from "../index.tsx";

const PREVIEW_FIXTURES = [
  {
    title: "Review inbox for replies",
    serverId: "haku_routine",
    toolName: "launch_routine",
    args: { text: "Scan Gmail for anything needing a reply, draft responses, and flag time-sensitive items." },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
