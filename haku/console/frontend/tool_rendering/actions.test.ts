import { describe, expect, it } from "vitest";

import { toolActionDescription } from "./actions.ts";
import { GMAIL_SERVER_ID, HOSTEXEC_SERVER_ID, KUBECTL_SERVER_ID, TANA_RW_SERVER_ID } from "./server_ids.ts";

describe("toolActionDescription", () => {
  it("describes a call from its arguments, for both generated and hand-authored schemas", () => {
    expect(
      toolActionDescription(GMAIL_SERVER_ID, "threads_modify_labels", {
        thread_ids: ["t1", "t2"],
        add: ["urgent"],
        remove: [],
      })?.text
    ).toBe("Gmail: Relabel 2 threads");
    expect(
      toolActionDescription(HOSTEXEC_SERVER_ID, "bash", {
        host: "wyrm2",
        run_as: "root",
        cmd: "id",
        max_bytes: 100_000,
        timeout_ms: 30_000,
        cwd: null,
      })?.text
    ).toBe("hostexec: Run on wyrm2 as root");
    // kubectl's schema is hand-authored (a remote server, absent from the generated catalog) and
    // shared with the widget rather than restated here.
    expect(
      toolActionDescription(KUBECTL_SERVER_ID, "resources_delete", {
        apiVersion: "v1",
        kind: "ConfigMap",
        name: "old",
      })
    ).toEqual({ text: "kubectl: Delete ConfigMap", destructive: true });
  });

  it("flags destructive calls, which a notification must say in words", () => {
    expect(toolActionDescription(TANA_RW_SERVER_ID, "trash_node", { nodeId: "n1" })?.destructive).toBe(true);
    expect(toolActionDescription(GMAIL_SERVER_ID, "drafts_create", {})?.destructive).toBeUndefined();
  });

  it("returns null for an unregistered tool, so callers fall back to serverId.toolName", () => {
    expect(toolActionDescription("nope", "whatever", {})).toBeNull();
    expect(toolActionDescription(GMAIL_SERVER_ID, "not_a_tool", {})).toBeNull();
  });

  it("returns null when arguments do not parse — a pending call's are not yet validated", () => {
    expect(toolActionDescription(HOSTEXEC_SERVER_ID, "bash", { host: 42 })).toBeNull();
  });

  it("describes argument-independent tools without consulting the arguments at all", () => {
    // These carry no schema, so a malformed payload still yields the right line.
    expect(toolActionDescription(TANA_RW_SERVER_ID, "move_node", { nonsense: true })?.text).toBe("Tana: Move node");
  });
});
