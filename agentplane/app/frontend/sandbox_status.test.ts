import { describe, expect, it } from "vitest";

import type { SandboxView } from "./client";
import { sandboxReady, sandboxSummary } from "./sandbox_status";

const UID = "00000000-0000-4000-8000-000000000001";

function sandboxWithPod(podName: string, deleting = false): SandboxView {
  return {
    name: "test-sandbox",
    uid: UID,
    namespace: "agentplane-test",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode: "Running",
    service_account: { namespace: "agentplane-test", name: "test-sandbox" },
    status: null,
    phase: "Running",
    pod: {
      name: podName,
      namespace: "agentplane-test",
      uid: `${UID}-pod`,
      deleting,
      node_name: "test-node",
      owner_references: [],
      status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
    },
  };
}

describe("a Sandbox whose incarnation Pod is Ready", () => {
  it.each([
    ["a runner Pod named for it", "test-sandbox"],
    ["a VM's virt-launcher Pod", "virt-launcher-test-sandbox-x7k2p"],
  ])("is ready with %s", (_kind, podName) => {
    const sandbox = sandboxWithPod(podName);
    expect(sandboxReady(sandbox)).toBe(true);
    expect(sandboxSummary(sandbox).kind).toBe("ready");
  });

  it("is not ready while the Pod is deleting", () => {
    expect(sandboxReady(sandboxWithPod("test-sandbox", true))).toBe(false);
  });
});

describe("a Sandbox's summary follows its phase over its Pod", () => {
  it.each([
    ["Sealing", { label: "Sealing", kind: "pending" }],
    ["Tearing down", { label: "Deleting", kind: "gone" }],
    ["Suspending", { label: "Suspending", kind: "pending" }],
    ["Suspended", { label: "Suspended", kind: "suspended" }],
  ] as const)("%s", (phase, summary) => {
    const sandbox = { ...sandboxWithPod("test-sandbox"), phase };
    expect(sandboxSummary(sandbox)).toEqual(summary);
    expect(sandboxReady(sandbox)).toBe(false);
  });
});
