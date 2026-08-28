// Node names returned by tana_rw_read_node for this server's preview fixtures.
export const SAMPLE_TANA_NODES: { nodes: { id: string; name: string }[] } = {
  nodes: [
    { id: "inbox", name: "Inbox" },
    { id: "task", name: "Quarterly planning" },
    { id: "project", name: "Console project" },
    { id: "old-parent", name: "Backlog" },
  ],
};

export const TANA_MCP_FIXTURES = {
  tana_rw__read_node: (args: Record<string, unknown>): string => {
    const input = args.input as { nodeId?: unknown } | undefined;
    const node = SAMPLE_TANA_NODES.nodes.find((candidate) => candidate.id === input?.nodeId);
    return node ? `- ${node.name} <!-- node-id: ${node.id} -->` : "";
  },
};
