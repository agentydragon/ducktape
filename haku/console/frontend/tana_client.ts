import { callOperatorMcpTool } from "./mcp_client.ts";

export type TanaNodePreview = { id: string; name: string };

const nodeMarker = /^(.*?)\s*<!-- node-id: ([^ ]+) -->\s*$/gm;
const bulletPrefix = /^\s*(?:-\s+)?(?:\[[ Xx]\](?:\s+|$))?/;

export function nodeNameFromMarkdown(markdown: string, nodeId: string): string | null {
  for (const match of markdown.matchAll(nodeMarker)) {
    if (match[2] === nodeId) {
      const name = match[1].replace(bulletPrefix, "").trim();
      return name || null;
    }
  }
  return null;
}

async function fetchTanaNodePreview(nodeId: string): Promise<TanaNodePreview | null> {
  try {
    // read_node is globally approval-shaped for Agent callers. The Operator branch accepts the
    // same advertised envelope but executes its `input` directly without creating a row.
    const payload = await callOperatorMcpTool("tana_rw_read_node", {
      input: { nodeId, maxDepth: 0 },
      rationale: "Resolve a Tana node name for an operator approval preview",
    });
    if (typeof payload !== "string") return null;
    const name = nodeNameFromMarkdown(payload, nodeId);
    return name === null ? null : { id: nodeId, name };
  } catch (error) {
    console.warn(`Could not resolve Tana node ${nodeId}`, error);
    return null;
  }
}

export async function fetchTanaNodePreviews(nodeIds: string[]): Promise<Record<string, TanaNodePreview>> {
  const uniqueIds = [...new Set(nodeIds)];
  const previews = await Promise.all(uniqueIds.map(fetchTanaNodePreview));
  return Object.fromEntries(
    previews.filter((preview): preview is TanaNodePreview => preview !== null).map((p) => [p.id, p])
  );
}
