import { describe, expect, it } from "vitest";

import { mcpToolSchema, mcpToolSchemas, type McpToolArgumentsFor } from "./mcp_tool_schema";

describe("generated MCP tool argument schemas", () => {
  it("constructs a validator for every advertised tool", () => {
    expect(mcpToolSchemas.length).toBeGreaterThan(0);
    const keys = mcpToolSchemas.map(({ serverId, toolName }) => `${serverId}.${toolName}`);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain("gmail.drafts_create");
    expect(keys).toContain("gmail.threads_modify_labels");
    expect(keys).toContain("google_calendar.create_event");
    expect(keys).toContain("google_calendar.get_event");
    expect(keys).toContain("google_calendar.list_events");
    expect(keys).toContain("google_calendar.list_event_instances");
  });

  it("keeps defaulted FastMCP parameters optional in the generated type and validator", () => {
    const args: McpToolArgumentsFor<"gmail", "drafts_create"> = {
      to: ["operator@example.com"],
      subject: "Hello",
      body: "Body",
    };
    expect(mcpToolSchema("gmail", "drafts_create").safeParse(args).success).toBe(true);
  });

  it("accepts explicit null where the FastMCP signature accepts it", () => {
    const gmailArgs: McpToolArgumentsFor<"gmail", "threads_modify_labels"> = {
      thread_ids: ["thread-1"],
      add: ["Follow up"],
      remove: null,
    };
    const calendarArgs: McpToolArgumentsFor<"google_calendar", "create_event"> = {
      summary: "Standup",
      start: { date: "2026-09-15", date_time: null, time_zone: null },
      end: { date: "2026-09-16", date_time: null, time_zone: null },
      reminders: null,
      attendees: null,
      recurrence: null,
    };

    expect(mcpToolSchema("gmail", "threads_modify_labels").safeParse(gmailArgs).success).toBe(true);
    expect(mcpToolSchema("google_calendar", "create_event").safeParse(calendarArgs).success).toBe(true);
  });

  it("rejects unknown top-level arguments like FastMCP", () => {
    expect(
      mcpToolSchema("gmail", "drafts_create").safeParse({
        to: ["operator@example.com"],
        subject: "Hello",
        body: "Body",
        unexpected: true,
      }).success
    ).toBe(false);
  });
});
