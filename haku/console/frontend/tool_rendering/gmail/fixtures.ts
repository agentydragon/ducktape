import type { McpToolResultFor } from "../../mcp_tool_result_schema.ts";

type GmailThreadPreviewsResponse = McpToolResultFor<"gmail", "thread_previews">;

// Live thread metadata returned by gmail_thread_previews for preview fixtures.
export const SAMPLE_GMAIL_THREADS = {
  t1: {
    subject: "Q3 planning — notes + open questions",
    snippet: "Here are the notes and open questions from the Q3 planning session.",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t1",
    current_label_names: ["Inbox", "Work"],
  },
  t2: {
    subject: "Re: dentist appointment confirmation",
    snippet: "Your appointment is confirmed for Tuesday morning.",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t2",
    current_label_names: ["Inbox"],
  },
  t3: {
    subject: "Your Thrive Market order shipped",
    snippet: "Your order is on its way and should arrive this week.",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t3",
    current_label_names: ["Inbox", "Receipts"],
  },
  t4: {
    subject: "This week in your neighborhood",
    snippet: "Events and updates from around the neighborhood this week.",
    gmail_url: "https://mail.google.com/mail/u/0/#all/t4",
    current_label_names: ["Inbox", "Newsletters"],
  },
} satisfies GmailThreadPreviewsResponse["threads"];

export const GMAIL_MCP_FIXTURES = {
  gmail_thread_previews: () => ({ threads: SAMPLE_GMAIL_THREADS }),
};
