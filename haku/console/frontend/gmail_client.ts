import { callOperatorMcpTool } from "./mcp_client";
import { mcpToolResultSchema } from "./mcp_tool_result_schema";

const zGmailThread = mcpToolResultSchema("gmail", "threads_get");
const zGmailMessage = mcpToolResultSchema("gmail", "messages_get");
const zGmailLabels = mcpToolResultSchema("gmail", "labels_list");

export type GmailLinkPreview = { subject: string | null; gmail_url: string };

export function gmailThreadUrl(threadId: string): string {
  return `https://mail.google.com/mail/u/0/#all/${threadId}`;
}

// Structural, not tied to one generated result type, so it fits threads_get's and messages_get's
// message shapes alike. Fields are optional to match the discovery-derived result schemas (Google
// marks almost everything optional).
type MessageWithHeaders = {
  payload?: { headers?: { name?: string | null; value?: string | null }[] | null } | null;
};

/** The `Subject` header's value off a Gmail message, case-insensitively; `null` when absent
 * (a `metadata`/`minimal` fetch that didn't request headers, or a message with none). */
export function messageSubject(message: MessageWithHeaders | null | undefined): string | null {
  return message?.payload?.headers?.find((header) => header.name?.toLowerCase() === "subject")?.value ?? null;
}

/** Every Gmail label's display name by id, for resolving a message/thread's opaque `labelIds`. */
export async function fetchGmailLabelNames(): Promise<ReadonlyMap<string, string>> {
  const labels = zGmailLabels.parse(await callOperatorMcpTool("gmail__labels_list", {}));
  return new Map(
    // id/name are optional in the discovery schema, though Gmail always returns both for a label.
    (labels.labels ?? [])
      .map((label) => [label.id, label.name] as const)
      .filter((entry): entry is [string, string] => entry[0] !== undefined && entry[1] !== undefined)
  );
}

/** Resolve the subject of the thread named in a threads_get request. */
export async function fetchGmailThreadPreview(threadId: string): Promise<GmailLinkPreview | null> {
  try {
    const payload = await callOperatorMcpTool("gmail__threads_get", { id: threadId, format: "metadata" });
    const thread = zGmailThread.parse(payload);
    return { subject: messageSubject(thread.messages?.[0]), gmail_url: gmailThreadUrl(threadId) };
  } catch (error) {
    console.warn(`Could not resolve Gmail thread ${threadId}`, error);
    return null;
  }
}

export async function fetchGmailMessagePreview(messageId: string): Promise<GmailLinkPreview | null> {
  try {
    const payload = await callOperatorMcpTool("gmail__messages_get", { id: messageId, format: "metadata" });
    const message = zGmailMessage.parse(payload);
    return {
      subject: messageSubject(message),
      gmail_url: gmailThreadUrl(message.threadId ?? messageId),
    };
  } catch (error) {
    console.warn(`Could not resolve Gmail message ${messageId}`, error);
    return null;
  }
}
