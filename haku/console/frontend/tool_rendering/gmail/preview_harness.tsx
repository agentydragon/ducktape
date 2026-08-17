// `gmail` preview screenshot entry — esbuild bundles this into the `:previews` IIFE. Holds the
// fixtures plus the mount call, with the Gmail-only fetch stub imported before the widget graph.
// `satisfies RegisteredToolPreviewFixture` ties each (serverId, toolName, args, result?) to the
// registry's real Zod schemas, so a stale id, argument, or result shape is a type error.
import "./preview_mock";

import { mountPreviewCards } from "../screenshot/mount";

import type { RegisteredToolPreviewFixture } from "../index";

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
      message: {
        id: "18c9f7a2b3d4e5f6",
        threadId: "thread-42",
        labelIds: ["DRAFT"],
        payload: { headers: [{ name: "Subject", value: "Re: Q3 planning" }] },
      },
    },
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
    title: "Search threads mentioning receipts",
    serverId: "gmail",
    toolName: "threads_list",
    args: { q: "label:receipts after:2026/01/01" },
    result: {
      threads: [
        { id: "t3", snippet: "Your order is on its way and should arrive this week." },
        { id: "t5", snippet: "Your receipt for order #48213 is attached." },
      ],
      nextPageToken: "next-page",
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
] satisfies (RegisteredToolPreviewFixture & { title: string })[];

mountPreviewCards(PREVIEW_FIXTURES);
