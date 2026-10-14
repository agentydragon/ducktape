import { describe, expect, it } from "vitest";

import type { SandboxView } from "./client";
import { sandboxReady, sandboxSummary } from "./sandbox_status";

const UID = "00000000-0000-4000-8000-000000000001";

function sandbox(conditions: { type: string; status: string }[]): SandboxView {
  return {
    name: "test-sandbox",
    uid: UID,
    namespace: "agentplane-test",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode: "Running",
    service_account: { namespace: "agentplane-test", name: "test-sandbox" },
    status: { conditions },
    phase: "Running",
    pod: {
      name: "test-sandbox-launcher",
      namespace: "agentplane-test",
      uid: `${UID}-pod`,
      deleting: false,
      node_name: "test-node",
      owner_references: [],
      status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
    },
  };
}

describe("a Sandbox is ready when the Sandbox Controller says so", () => {
  it("reads its own Ready condition, whatever Pod hosts the runner", () => {
    const ready = sandbox([{ type: "Ready", status: "True" }]);
    expect(sandboxReady(ready)).toBe(true);
    expect(sandboxSummary(ready).kind).toBe("ready");
  });

  it.each([
    ["Ready is False", [{ type: "Ready", status: "False" }]],
    ["it has no Ready condition", [{ type: "Running", status: "True" }]],
  ])("is not ready when %s, even with a Ready Pod", (_, conditions) => {
    expect(sandboxReady(sandbox(conditions))).toBe(false);
  });
});

describe("a Sandbox's summary follows its phase over its Pod", () => {
  it.each([
    ["Quiescing", { label: "Quiescing", kind: "pending" }],
    ["Tearing down", { label: "Deleting", kind: "gone" }],
    ["Suspending", { label: "Suspending", kind: "pending" }],
    ["Suspended", { label: "Suspended", kind: "suspended" }],
  ] as const)("%s", (phase, summary) => {
    expect(sandboxSummary({ ...sandbox([{ type: "Ready", status: "False" }]), phase })).toEqual(summary);
  });
});
