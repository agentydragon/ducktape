import { describe, expect, it } from "vitest";

import { toolActionDescription } from "./actions";
import { GMAIL_SERVER_ID, KUBECTL_SERVER_ID, TANA_RW_SERVER_ID } from "./server_ids";

describe("toolActionDescription", () => {
  it("describes a call from its arguments, for both generated and hand-authored schemas", () => {
    expect(
      toolActionDescription(GMAIL_SERVER_ID, "threads_modify_labels", {
        thread_ids: ["t1", "t2"],
        add: ["urgent"],
        remove: [],
      })?.text
    ).toBe("Gmail: Relabel 2 threads");
    // kubectl's schema is hand-authored (a remote server, absent from the generated catalog) and
    // shared with the widget rather than restated here.
    expect(
      toolActionDescription(KUBECTL_SERVER_ID, "resources_delete", {
        apiVersion: "v1",
        kind: "ConfigMap",
        name: "old",
      })
    ).toEqual({ text: "kubectl: Delete ConfigMap", destructive: true });
    expect(
      toolActionDescription(KUBECTL_SERVER_ID, "resources_get", {
        apiVersion: "apps/v1",
        kind: "Deployment",
        name: "api",
        namespace: "prod",
      })
    ).toEqual({ text: "kubectl: Get Deployment" });
    expect(
      toolActionDescription(KUBECTL_SERVER_ID, "pods_list_in_namespace", {
        namespace: "prod",
        labelSelector: "app=api",
      })
    ).toEqual({ text: "kubectl: List Pods in prod" });
    expect(
      toolActionDescription(KUBECTL_SERVER_ID, "pods_exec", {
        name: "api-0",
        namespace: "prod",
        command: ["sh", "-c", "echo hello"],
      })
    ).toEqual({ text: "kubectl: Exec in prod/api-0" });
  });

  it("flags destructive calls, which a notification must say in words", () => {
    expect(toolActionDescription(TANA_RW_SERVER_ID, "trash_node", { nodeId: "n1" })?.destructive).toBe(true);
    expect(toolActionDescription(GMAIL_SERVER_ID, "drafts_create", {})?.destructive).toBeUndefined();
  });

  it("returns null for an unregistered tool, so callers fall back to serverId.toolName", () => {
    expect(toolActionDescription("nope", "whatever", {})).toBeNull();
    expect(toolActionDescription(GMAIL_SERVER_ID, "not_a_tool", {})).toBeNull();
  });

  it("describes argument-independent tools without consulting the arguments at all", () => {
    // These carry no schema, so a malformed payload still yields the right line.
    expect(toolActionDescription(TANA_RW_SERVER_ID, "move_node", { nonsense: true })?.text).toBe("Tana: Move node");
  });
});
