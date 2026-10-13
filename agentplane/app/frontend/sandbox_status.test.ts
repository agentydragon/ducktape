import { describe, expect, it } from "vitest";

import type { SandboxView } from "./client";
import { sandboxReady, sandboxSummary } from "./sandbox_status";

const UID = "00000000-0000-4000-8000-000000000001";

function sandboxOwnedBy(apiVersion: string): SandboxView {
  return {
    name: "test-sandbox",
    uid: UID,
    namespace: "agentplane-test",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode: "Running",
    service_account: { namespace: "agentplane-test", name: "test-sandbox" },
    status: null,
    deleting: false,
    pod: {
      name: "test-sandbox",
      namespace: "agentplane-test",
      uid: `${UID}-pod`,
      deleting: false,
      node_name: "test-node",
      owner_references: [
        { api_version: apiVersion, kind: "Sandbox", name: "test-sandbox", uid: UID, controller: true },
      ],
      status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
    },
  };
}

describe("a Sandbox whose running Pod is Ready", () => {
  it("is ready when Agentplane's Sandbox controls the Pod", () => {
    const sandbox = sandboxOwnedBy("agentplane.allegedly.works/v1alpha1");
    expect(sandboxReady(sandbox)).toBe(true);
    expect(sandboxSummary(sandbox).kind).toBe("ready");
  });

  it("is not ready when another API group's Sandbox controls the Pod", () => {
    expect(sandboxReady(sandboxOwnedBy("other.example/v1"))).toBe(false);
  });
});
