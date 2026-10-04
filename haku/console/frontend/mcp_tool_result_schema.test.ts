import { describe, expect, it } from "vitest";

import { mcpToolResultSchema, type McpToolResultFor } from "./mcp_tool_result_schema";

describe("generated MCP tool result schemas", () => {
  it("parses a configured server's connection status", () => {
    const result: McpToolResultFor<"haku-console", "list_mcp_servers"> = {
      servers: [
        {
          server_id: "grants",
          backend: { kind: "in_process", credential: { kind: "none" } },
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
