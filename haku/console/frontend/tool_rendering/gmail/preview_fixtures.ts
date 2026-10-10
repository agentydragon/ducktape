// `gmail` preview fixtures: the only list of this server's preview cards. The `:previews` target
// mounts them (`preview_harness.tsx`) and derives its screenshot scenarios from them
// (`screenshot/emit_scenarios.mjs`). `RegisteredToolPreviewFixture` ties each (serverId, toolName,
// args, result?) to the registry's real Zod schemas, so a stale id, argument, or result shape is a
// type error.
import type { RegisteredToolPreviewFixture } from "../index";

export const PREVIEW_FIXTURES: (RegisteredToolPreviewFixture & { title: string })[] = [
  {
    title: "File planning threads for follow-up",
    serverId: "gmail",
    toolName: "threads_modify_labels",
    args: { thread_ids: ["t1", "t2", "t3", "t4"], add: ["Follow up"], remove: ["Inbox"] },
  },
  {
    title: "Get the Q3 planning thread",
    serverId: "gmail",
    toolName: "threads_get",
    args: { id: "t1", format: "full" },
    result: {
      id: "t1",
      snippet: "Here are the notes and open questions from the Q3 planning session.",
      messages: [
        {
          id: "m-t1",
          threadId: "t1",
          labelIds: ["INBOX", "Label_Work"],
          snippet: "Here are the notes and open questions from the Q3 planning session.",
          payload: { headers: [{ name: "Subject", value: "Q3 planning — notes + open questions" }] },
        },
      ],
    },
  },
  {
    title: "Get the dentist confirmation message",
    serverId: "gmail",
    toolName: "messages_get",
    args: { id: "m-t2", format: "full" },
    result: {
      id: "m-t2",
      threadId: "t2",
      labelIds: ["INBOX"],
      snippet: "Your appointment is confirmed for Tuesday morning.",
      payload: { headers: [{ name: "Subject", value: "Re: dentist appointment confirmation" }] },
    },
  },
];
