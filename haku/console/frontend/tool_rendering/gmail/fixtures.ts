import type { McpToolResultFor } from "../../mcp_tool_result_schema";

type GmailThread = McpToolResultFor<"gmail", "threads_get">;
type GmailMessage = McpToolResultFor<"gmail", "messages_get">;
type GmailLabels = McpToolResultFor<"gmail", "labels_list">;

const GMAIL_LABELS: GmailLabels = {
  labels: [
    { id: "INBOX", name: "Inbox", type: "system" },
    { id: "Label_Work", name: "Work", type: "user" },
    { id: "Label_Receipts", name: "Receipts", type: "user" },
    { id: "Label_Newsletters", name: "Newsletters", type: "user" },
  ],
};

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

// Every thread fixture's own first message, keyed by its `m-<threadId>` id (the `thread()`
// helper's convention) — so a `messages_get` fixture can reuse the same content a `threads_get`
// fixture already carries, instead of a second hand-authored copy.
const GMAIL_MESSAGES: Readonly<Record<string, GmailMessage>> = Object.fromEntries(
  Object.values(GMAIL_THREADS)
    .map((t) => t.messages?.[0])
    .filter((m): m is NonNullable<typeof m> => m !== undefined)
    .map((m) => [m.id, m])
);

export const GMAIL_MCP_FIXTURES = {
  gmail__labels_list: (): typeof GMAIL_LABELS => GMAIL_LABELS,
  gmail__threads_get: (args: Record<string, unknown>): GmailThread => {
    const threadId = args.id;
    const result = typeof threadId === "string" ? GMAIL_THREADS[threadId] : undefined;
    if (result === undefined) throw new Error(`No Gmail thread fixture for ${String(threadId)}`);
    return result;
  },
  gmail__messages_get: (args: Record<string, unknown>): GmailMessage => {
    const messageId = args.id;
    const result = typeof messageId === "string" ? GMAIL_MESSAGES[messageId] : undefined;
    if (result === undefined) throw new Error(`No Gmail message fixture for ${String(messageId)}`);
    return result;
  },
};
