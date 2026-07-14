// `gmail` preview screenshot entry — esbuild bundles this into the `:previews` IIFE. Holds the
// fixtures plus the mount call; `mount` is imported FIRST so its fetch stub (mock.ts) is installed
// before the registry/widget graph reaches client.ts's openapi-fetch. `satisfies
// RegisteredToolPreviewFixture` ties each (serverId, toolName, args, result?) to the registry's
// real Zod schemas, so a stale id, argument, or result shape is a type error.
import { mountPreviewCards } from "../screenshot/mount.tsx";

import type { RegisteredToolPreviewFixture } from "../index.tsx";

const PREVIEW_FIXTURES = [
  {
    title: "File planning threads for follow-up",
    serverId: "gmail",
    toolName: "threads_modify_labels",
    args: { thread_ids: ["t1", "t2", "t3", "t4"], add: ["Follow up"], remove: ["Inbox"] },
  },
  {
    title: "Draft Q3 planning reply",
    serverId: "gmail",
    toolName: "drafts_create",
    args: {
      to: ["ops@allegedly.works"],
      cc: ["rai@allegedly.works"],
      subject: "Re: Q3 planning",
      body: "Hi team,\n\nThanks for the notes. A few thoughts on the roadmap:\n- Ship the console Settings panel\n- Then the previews gallery\n- Circle back on datetime formatting\n\nBest,\nRai",
      thread_id: "thread-42",
    },
    result: {
      id: "r-2603837261749773001",
      message: { id: "18c9f7a2b3d4e5f6", threadId: "thread-42", labelIds: ["DRAFT"] },
    },
  },
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
