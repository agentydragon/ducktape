import { callOperatorMcpTool } from "./mcp_client";
import { mcpToolResultSchema, type McpToolResultFor } from "./mcp_tool_result_schema";

type GmailThread = McpToolResultFor<"gmail", "threads_get">;
type GmailLabels = McpToolResultFor<"gmail", "labels_list">;

const zGmailThread = mcpToolResultSchema("gmail", "threads_get");
const zGmailMessage = mcpToolResultSchema("gmail", "messages_get");
const zGmailLabels = mcpToolResultSchema("gmail", "labels_list");

export type GmailThreadPreview = {
  subject: string | null;
  snippet: string;
  current_label_names: string[];
  gmail_url: string;
};

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

export function gmailThreadPreview(
  threadId: string,
  thread: GmailThread,
  labelNamesById: ReadonlyMap<string, string>
): GmailThreadPreview {
  const firstMessage = thread.messages?.[0];
  const currentLabelNames = (firstMessage?.labelIds ?? [])
    .map((labelId) => labelNamesById.get(labelId))
    .filter((name): name is string => name !== undefined)
    .sort();
  return {
    subject: messageSubject(firstMessage),
    snippet: firstMessage?.snippet ?? "",
    current_label_names: currentLabelNames,
    gmail_url: `https://mail.google.com/mail/u/0/#all/${threadId}`,
  };
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

// Compose the Gmail server's ordinary reads in the browser. Operator calls execute directly
// through /mcp, so the enrichment creates no approval or audit row.
export async function fetchGmailThreadPreviews(threadIds: string[]): Promise<Record<string, GmailThreadPreview>> {
  if (threadIds.length === 0) return {};
  const uniqueIds = [...new Set(threadIds)];
  const [labelNamesById, threads] = await Promise.all([
    fetchGmailLabelNames(),
    Promise.all(
      uniqueIds.map(async (threadId): Promise<readonly [string, GmailThread | null]> => {
        try {
          const payload = await callOperatorMcpTool("gmail__threads_get", { id: threadId, format: "metadata" });
          return [threadId, zGmailThread.parse(payload)] as const;
        } catch (error) {
          console.warn(`Could not resolve Gmail thread ${threadId}`, error);
          return [threadId, null] as const;
        }
      })
    ),
  ]);
  const previews: Record<string, GmailThreadPreview> = {};
  for (const [threadId, thread] of threads) {
    if (thread !== null) previews[threadId] = gmailThreadPreview(threadId, thread, labelNamesById);
  }
  return previews;
}

export type GmailMessagePreview = { subject: string | null; snippet: string; gmail_url: string };

// A single message's subject/snippet, for a `messages_get` call's own pending-args identity
// preview — the raw message id alone isn't user-readable. No label-name resolution (unlike
// fetchGmailThreadPreviews): this is a lone identity line, not a list with label pills.
export async function fetchGmailMessagePreview(messageId: string): Promise<GmailMessagePreview | null> {
  try {
    const payload = await callOperatorMcpTool("gmail__messages_get", { id: messageId, format: "metadata" });
    const message = zGmailMessage.parse(payload);
    return {
      subject: messageSubject(message),
      snippet: message.snippet ?? "",
      gmail_url: `https://mail.google.com/mail/u/0/#all/${message.threadId ?? messageId}`,
    };
  } catch (error) {
    console.warn(`Could not resolve Gmail message ${messageId}`, error);
    return null;
  }
}
