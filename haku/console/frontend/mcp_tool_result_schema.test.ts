import { describe, expect, it } from "vitest";

import { mcpToolResultSchema, mcpToolResultSchemas, type McpToolResultFor } from "./mcp_tool_result_schema.ts";

describe("generated MCP tool result schemas", () => {
  it("constructs a validator for every advertised result tool", () => {
    expect(mcpToolResultSchemas.length).toBeGreaterThan(0);
    const keys = mcpToolResultSchemas.map(({ serverId, toolName }) => `${serverId}.${toolName}`);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain("gmail.drafts_create");
    expect(keys).toContain("google_calendar.create_event");
    expect(keys).toContain("google_calendar.get_event");
    expect(keys).toContain("google_calendar.list_events");
    expect(keys).toContain("google_calendar.list_event_instances");
    // A `-> None` return has no structured result, so it has no entry.
    expect(keys).not.toContain("gmail.labels_delete");
    // grocy-sf's batch-tool result schemas are now reflected (the allowlisted preview tools).
    expect(keys.filter((key) => key.startsWith("grocy-sf."))).toHaveLength(12);
    expect(keys).toContain("grocy-sf.stock_add");
    expect(keys).toContain("grocy-sf.products_list");
  });

  it("parses a Draft resource and rejects one missing its id", () => {
    const draft: McpToolResultFor<"gmail", "drafts_create"> = {
      id: "r-7364618394",
      // gmail_api's to_camel wire aliases (threadId, not thread_id); the recursive `parts`
      // tree under payload is permissive, so this shallow message parses.
      message: { id: "18c2f0a", threadId: "t42" },
    };
    expect(mcpToolResultSchema("gmail", "drafts_create").safeParse(draft).success).toBe(true);
    expect(mcpToolResultSchema("gmail", "drafts_create").safeParse({ message: { id: "m1" } }).success).toBe(false);
  });

  it("parses a created calendar event", () => {
    const result: McpToolResultFor<"google_calendar", "create_event"> = {
      event_id: "evt-1",
      html_link: "https://calendar.google.com/evt-1",
    };
    expect(mcpToolResultSchema("google_calendar", "create_event").safeParse(result).success).toBe(true);
    expect(mcpToolResultSchema("google_calendar", "get_event").safeParse(result).success).toBe(true);
    expect(mcpToolResultSchema("google_calendar", "list_events").safeParse({ events: [result] }).success).toBe(true);
  });
});
