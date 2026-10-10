import { describe, expect, it } from "vitest";

import { toolActionDescription } from "./actions";
import { GMAIL_SERVER_ID, TANA_SERVER_ID } from "./server_ids";

describe("toolActionDescription", () => {
  it("describes a call from its generated schema", () => {
    expect(
      toolActionDescription(GMAIL_SERVER_ID, "threads_modify_labels", {
        thread_ids: ["t1", "t2"],
        add: ["urgent"],
        remove: [],
      })?.text
    ).toBe("Gmail: Relabel 2 threads");
  });

  it("describes a call from its hand-authored schema", () => {
    expect(
      toolActionDescription(TANA_SERVER_ID, "set_field_option", {
        nodeId: "node",
        attributeId: "status",
        optionId: "done",
        mode: "append",
      })?.text
    ).toBe("Tana: Append field option");
  });

  it("flags destructive calls, which a notification must say in words", () => {
    expect(toolActionDescription(TANA_SERVER_ID, "trash_node", { nodeId: "n1" })?.destructive).toBe(true);
  });

  it("returns null for an unregistered tool, so callers fall back to serverId.toolName", () => {
    expect(toolActionDescription("nope", "whatever", {})).toBeNull();
    expect(toolActionDescription(GMAIL_SERVER_ID, "not_a_tool", {})).toBeNull();
  });

  it("describes argument-independent tools without consulting the arguments at all", () => {
    // These carry no schema, so a malformed payload still yields the right line.
    expect(toolActionDescription(TANA_SERVER_ID, "move_node", { nonsense: true })?.text).toBe("Tana: Move node");
  });
});
