import { describe, expect, it } from "vitest";

import type { SandboxView } from "./api/schema";
import { hasVmConsole, sandboxReady, sandboxStatusDetail, sandboxSummary } from "./sandbox_status";

const UID = "00000000-0000-4000-8000-000000000001";

function sandbox(conditions: { type: string; status: string }[]): SandboxView {
  return {
    name: "test-sandbox",
    uid: UID,
    namespace: "agentplane-test",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode: "Running",
    wake_on_send: false,
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

describe("a Sandbox exposes the VM console only for a VMI-backed incarnation", () => {
  it("distinguishes its own Pod from a KubeVirt launcher Pod", () => {
    const podSandbox = sandbox([{ type: "Ready", status: "True" }]);
    const vmSandbox = {
      ...podSandbox,
      pod: {
        name: "test-sandbox-launcher",
        namespace: "agentplane-test",
        uid: "vmi-launcher-pod",
        deleting: false,
        node_name: "test-node",
        owner_references: [
          {
            api_version: "kubevirt.io/v1",
            kind: "VirtualMachineInstance",
            name: "test-sandbox",
            uid: "vmi-uid",
            controller: true,
          },
        ],
        status: { phase: "Running" },
      },
    } satisfies SandboxView;
    expect(hasVmConsole(podSandbox)).toBe(false);
    expect(hasVmConsole(vmSandbox)).toBe(true);
    expect(hasVmConsole({ ...podSandbox, pod: null })).toBe(false);
  });
});

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

  it("does not report an unreconciled Sandbox as suspended", () => {
    expect(
      sandboxSummary({
        ...sandbox([{ type: "Ready", status: "True" }]),
        operating_mode: null,
        phase: "Unspecified",
        pod: null,
      })
    ).toEqual({
      label: "Status unknown",
      kind: "pending",
    });
    expect(sandboxReady({ ...sandbox([{ type: "Ready", status: "True" }]), operating_mode: null })).toBe(false);
  });
});

describe("a suspended Sandbox says why it is suspended", () => {
  const suspended: SandboxView = { ...sandbox([]), operating_mode: "Suspended", phase: "Suspended", pod: null };

  it("names an idle suspension and the time that triggered it", () => {
    const idle: SandboxView = { ...suspended, suspended_by: "Idle", idle_suspend_after_seconds: 5400 };
    expect(sandboxSummary(idle)).toEqual({ label: "Suspended (idle)", kind: "suspended" });
    expect(sandboxStatusDetail(idle)).toContain("Suspended because its runner was idle for 1 hour 30 minutes");
  });

  it("names a suspension a requester made", () => {
    const requested: SandboxView = { ...suspended, suspended_by: "Requested" };
    expect(sandboxSummary(requested)).toEqual({ label: "Suspended (on request)", kind: "suspended" });
    expect(sandboxStatusDetail(requested)).toContain("Suspended on request");
  });

  it("names it while the Sandbox is still suspending, and says nothing before the controller reports", () => {
    expect(sandboxSummary({ ...suspended, phase: "Suspending", suspended_by: "Idle" }).label).toBe("Suspending (idle)");
    expect(sandboxSummary(suspended).label).toBe("Suspended");
  });
});
