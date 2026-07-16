import type { McpToolResultFor } from "../../mcp_tool_result_schema.ts";

type GmailThread = McpToolResultFor<"gmail", "threads_get">;
type GmailLabels = McpToolResultFor<"gmail", "labels_list">;

const GMAIL_LABELS = {
  labels: [
    { id: "INBOX", name: "Inbox", type: "system" },
    { id: "Label_Work", name: "Work", type: "user" },
    { id: "Label_Receipts", name: "Receipts", type: "user" },
    { id: "Label_Newsletters", name: "Newsletters", type: "user" },
  ],
} satisfies GmailLabels;

function thread(id: string, subject: string, snippet: string, labelIds: string[]): GmailThread {
  return {
    id,
    snippet,
    messages: [
      {
        id: `m-${id}`,
        threadId: id,
        labelIds,
        snippet,
        payload: { headers: [{ name: "Subject", value: subject }] },
      },
    ],
  };
}

const GMAIL_THREADS: Readonly<Record<string, GmailThread>> = {
  t1: thread(
    "t1",
    "Q3 planning — notes + open questions",
    "Here are the notes and open questions from the Q3 planning session.",
    ["INBOX", "Label_Work"]
  ),
  t2: thread("t2", "Re: dentist appointment confirmation", "Your appointment is confirmed for Tuesday morning.", [
    "INBOX",
  ]),
  t3: thread("t3", "Your Thrive Market order shipped", "Your order is on its way and should arrive this week.", [
    "INBOX",
    "Label_Receipts",
  ]),
  t4: thread("t4", "This week in your neighborhood", "Events and updates from around the neighborhood this week.", [
    "INBOX",
    "Label_Newsletters",
  ]),
};

export const GMAIL_MCP_FIXTURES = {
  gmail_labels_list: () => GMAIL_LABELS,
  gmail_threads_get: (args: Record<string, unknown>) => {
    const threadId = args.thread_id;
    const result = typeof threadId === "string" ? GMAIL_THREADS[threadId] : undefined;
    if (result === undefined) throw new Error(`No Gmail thread fixture for ${String(threadId)}`);
    return result;
  },
};
