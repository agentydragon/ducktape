import { create } from "@bufbuild/protobuf";
import { DurationSchema } from "@bufbuild/protobuf/wkt";
import { describe, expect, it } from "vitest";

import { SandboxPhase, type Sandbox } from "../../proto/sandboxes_pb";
import { readySandbox, testSandbox } from "./testing/sandbox";
import type { SandboxView } from "./schema";
import {
  hasVmConsole,
  podIp,
  podPhase,
  sandboxIdleSuspendAfterSeconds,
  sandboxOperatingMode,
  sandboxPhase,
  sandboxReady,
  sandboxStatusDetail,
  sandboxSummary,
  sandboxSuspended,
  sandboxWakeOnSend,
} from "./sandbox_status";

const UID = "00000000-0000-4000-8000-000000000001";

function sandbox(conditions: { type: string; status: string }[]): SandboxView {
  return {
    name: "test-sandbox",
    uid: UID,
    namespace: "agentplane-test",
    created_at: "2026-01-01T00:00:00Z",
    binding: null,
    preset: null,
    operating_mode: "Running",
    wake_on_send: false,
    idle_suspend_after_seconds: null,
    idle_suspend_at: null,
    autosuspend_blockers: [],
    suspended_by: null,
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

function protoSandbox(overrides: Partial<Sandbox> = {}): Sandbox {
  return Object.assign(testSandbox("test-sandbox", UID), overrides);
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

describe("the shared status helpers accept the live Sandbox proto", () => {
  it("normalizes generated phases and keeps them separate from operating mode", () => {
    const unreconciled = protoSandbox({ phase: SandboxPhase.UNSPECIFIED, operatingMode: "Running" });
    expect(sandboxPhase(unreconciled)).toBe("Unspecified");
    expect(sandboxOperatingMode(unreconciled)).toBe("Running");
    expect(sandboxSummary(unreconciled)).toEqual({ label: "Status unknown", kind: "pending" });

    const suspended = protoSandbox({ phase: SandboxPhase.SUSPENDED, operatingMode: "Suspended" });
    expect(sandboxPhase(suspended)).toBe("Suspended");
    expect(sandboxSuspended(suspended)).toBe(true);
  });

  it("reads controller and Pod status, owner references, and common policy fields", () => {
    const running = readySandbox("test-sandbox", UID);
    running.wakeOnSend = true;
    const pod = running.pod;
    if (!pod) throw new Error("Missing ready Sandbox Pod");
    const owner = pod.ownerReferences[0];
    if (!owner) throw new Error("Missing ready Sandbox owner reference");
    owner.apiVersion = "kubevirt.io/v1";
    owner.kind = "VirtualMachineInstance";
    owner.uid = "vmi-uid";
    expect(sandboxReady(running)).toBe(true);
    expect(sandboxSummary(running)).toEqual({ label: "Ready", kind: "ready" });
    expect(hasVmConsole(running)).toBe(true);
    expect(podPhase(running)).toBe("Running");
    expect(podIp(running)).toBe("10.0.0.1");
    expect(sandboxWakeOnSend(running)).toBe(true);
    expect(sandboxWakeOnSend(protoSandbox())).toBe(false);
  });

  it("preserves duration presence, zero, and fractional seconds", () => {
    expect(sandboxIdleSuspendAfterSeconds(protoSandbox())).toBeNull();
    expect(
      sandboxIdleSuspendAfterSeconds(protoSandbox({ idleSuspendAfter: create(DurationSchema, { seconds: 0n }) }))
    ).toBe(0);
    expect(
      sandboxIdleSuspendAfterSeconds(
        protoSandbox({ idleSuspendAfter: create(DurationSchema, { seconds: 90n, nanos: 500_000_000 }) })
      )
    ).toBe(90.5);
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
