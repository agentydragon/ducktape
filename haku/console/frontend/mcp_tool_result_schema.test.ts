import { describe, expect, it } from "vitest";

import { mcpToolResultSchema, mcpToolResultSchemas, type McpToolResultFor } from "./mcp_tool_result_schema";

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
    expect(keys).not.toContain("gmail.thread_previews");
    expect(keys).not.toContain("google_calendar.calendar_summary");
    // A `-> None` return has no structured result, so it has no entry.
    expect(keys).not.toContain("gmail.labels_delete");
    // grocy-sf's batch-tool result schemas are now reflected (the allowlisted preview tools).
    expect(keys.filter((key) => key.startsWith("grocy-sf."))).toHaveLength(15);
    expect(keys).toContain("grocy-sf.stock_add");
    expect(keys).toContain("grocy-sf.products_list");
    expect(keys).toContain("grocy-sf.locations_list");
    expect(keys).toContain("grocy-sf.product_groups_list");
    expect(keys).toContain("grocy-sf.shopping_lists_list");
    expect(keys).toContain("haku-console.list_mcp_servers");
    expect(keys).toContain("haku-console.get_mcp_server_status");
    expect(keys).toContain("haku-console.list_node_daemons");
  });

  it("parses an unprovisioned connected-account status", () => {
    const result: McpToolResultFor<"haku-console", "list_mcp_servers"> = {
      servers: [
        {
          server_id: "google_calendar",
          backend: {
            kind: "in_process",
            credential: { kind: "operator_connection", connection: "google_calendar" },
          },
          connection: {
            connection: "google_calendar",
            display_name: "Google Calendar",
            provider: "google",
            status: "unprovisioned",
            detail: "OAuth client not provisioned on this console; see the console deployment README.",
          },
        },
      ],
    };
    expect(mcpToolResultSchema("haku-console", "list_mcp_servers").safeParse(result).success).toBe(true);
    expect(mcpToolResultSchema("haku-console", "list_mcp_servers").safeParse({}).success).toBe(false);
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
    expect(
      mcpToolResultSchema("google_calendar", "list_events").safeParse({ events: [result], summary: "Family" }).success
    ).toBe(true);
  });
});
