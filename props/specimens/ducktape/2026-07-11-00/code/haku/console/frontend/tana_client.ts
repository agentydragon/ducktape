import { api, errorDetail } from "./client.ts";
import type { components } from "./api/schema";

export type TanaNodePreview = components["schemas"]["TanaNodePreview"];

export async function fetchTanaNodePreviews(nodeIds: string[]): Promise<Record<string, TanaNodePreview>> {
  if (nodeIds.length === 0) return {};
  const { data, error } = await api.GET("/api/tana-rw/node-previews", {
    params: { query: { node_id: nodeIds } },
  });
  if (error || !data) throw new Error(errorDetail(error, "Failed to load Tana node names"));
  return Object.fromEntries(data.nodes.map((node) => [node.id, node]));
}
