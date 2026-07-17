import { callOperatorMcpTool } from "./mcp_client.ts";
import { mcpToolResultSchema, type McpToolResultFor } from "./mcp_tool_result_schema.ts";

type GmailThread = McpToolResultFor<"gmail", "threads_get">;
type GmailLabels = McpToolResultFor<"gmail", "labels_list">;

const zGmailThread = mcpToolResultSchema("gmail", "threads_get");
const zGmailLabels = mcpToolResultSchema("gmail", "labels_list");

export type GmailThreadPreview = {
  subject: string | null;
  snippet: string;
  current_label_names: string[];
  gmail_url: string;
};

// Structural, not tied to one generated result type, so it fits threads_get's and messages_get's
// message shapes alike.
type MessageWithHeaders = { payload?: { headers?: { name: string; value: string }[] | null } | null };

/** The `Subject` header's value off a Gmail message, case-insensitively; `null` when absent
 * (a `metadata`/`minimal` fetch that didn't request headers, or a message with none). */
export function messageSubject(message: MessageWithHeaders | null | undefined): string | null {
  return message?.payload?.headers?.find((header) => header.name.toLowerCase() === "subject")?.value ?? null;
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
  const labels = zGmailLabels.parse(await callOperatorMcpTool("gmail_labels_list", {}));
  return new Map((labels.labels ?? []).map((label) => [label.id, label.name]));
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
          const payload = await callOperatorMcpTool("gmail_threads_get", { thread_id: threadId, format: "metadata" });
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
