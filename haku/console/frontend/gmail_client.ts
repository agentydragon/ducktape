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

export function gmailThreadPreview(
  threadId: string,
  thread: GmailThread,
  labelNamesById: ReadonlyMap<string, string>
): GmailThreadPreview {
  const firstMessage = thread.messages?.[0];
  const subject = firstMessage?.payload?.headers?.find((header) => header.name.toLowerCase() === "subject")?.value;
  const currentLabelNames = (firstMessage?.labelIds ?? [])
    .map((labelId) => labelNamesById.get(labelId))
    .filter((name): name is string => name !== undefined)
    .sort();
  return {
    subject: subject ?? null,
    snippet: firstMessage?.snippet ?? "",
    current_label_names: currentLabelNames,
    gmail_url: `https://mail.google.com/mail/u/0/#all/${threadId}`,
  };
}

// Compose the Gmail server's ordinary reads in the browser. Operator calls execute directly
// through /mcp, so the enrichment creates no approval or audit row.
export async function fetchGmailThreadPreviews(threadIds: string[]): Promise<Record<string, GmailThreadPreview>> {
  if (threadIds.length === 0) return {};
  const uniqueIds = [...new Set(threadIds)];
  const [labels, threads] = await Promise.all([
    callOperatorMcpTool("gmail_labels_list", {}).then((payload) => zGmailLabels.parse(payload)),
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
  const labelNamesById = new Map((labels.labels ?? []).map((label) => [label.id, label.name]));
  const previews: Record<string, GmailThreadPreview> = {};
  for (const [threadId, thread] of threads) {
    if (thread !== null) previews[threadId] = gmailThreadPreview(threadId, thread, labelNamesById);
  }
  return previews;
}
