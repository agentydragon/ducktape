import { create } from "@bufbuild/protobuf";
import { DurationSchema } from "@bufbuild/protobuf/wkt";
import { describe, expect, it } from "vitest";

import { SandboxPhase, SandboxPodSchema } from "../../proto/sandboxes_pb";
import { OperatingMode, type EnvironmentDescriptor } from "../../sandboxes/environments/environment_service_pb";
import { testSandbox } from "./testing/sandbox";
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

function sandbox(conditions: { type: string; status: string }[]): EnvironmentDescriptor {
  const value = testSandbox("test-sandbox", UID);
  value.operatingMode = OperatingMode.RUNNING;
  value.phase = SandboxPhase.RUNNING;
  value.status = { conditions };
  value.pod = create(SandboxPodSchema, {
    name: "test-sandbox-launcher",
    namespace: "agentplane-test",
    uid: `${UID}-pod`,
    nodeName: "test-node",
    status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
  });
  return value;
}

function protoSandbox(overrides: Partial<EnvironmentDescriptor> = {}): EnvironmentDescriptor {
  return Object.assign(testSandbox("test-sandbox", UID), overrides);
}

describe("a Sandbox exposes the VM console only for a VMI-backed incarnation", () => {
  it("distinguishes its own Pod from a KubeVirt launcher Pod", () => {
    const podSandbox = sandbox([{ type: "Ready", status: "True" }]);
    const vmSandbox = {
      ...podSandbox,
      pod: create(SandboxPodSchema, {
        name: "test-sandbox-launcher",
        namespace: "agentplane-test",
        uid: "vmi-launcher-pod",
        nodeName: "test-node",
        ownerReferences: [
          {
            apiVersion: "kubevirt.io/v1",
            kind: "VirtualMachineInstance",
            name: "test-sandbox",
            uid: "vmi-uid",
            controller: true,
          },
        ],
        status: { phase: "Running" },
      }),
    };
    expect(hasVmConsole(podSandbox)).toBe(false);
    expect(hasVmConsole(vmSandbox)).toBe(true);
    expect(hasVmConsole({ ...podSandbox, pod: undefined })).toBe(false);
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
    const unreconciled = protoSandbox({ phase: SandboxPhase.UNSPECIFIED, operatingMode: OperatingMode.RUNNING });
    expect(sandboxPhase(unreconciled)).toBe("Unspecified");
    expect(sandboxOperatingMode(unreconciled)).toBe("Running");
    expect(sandboxSummary(unreconciled)).toEqual({ label: "Status unknown", kind: "pending" });

    const suspended = protoSandbox({ phase: SandboxPhase.SUSPENDED, operatingMode: OperatingMode.SUSPENDED });
    expect(sandboxPhase(suspended)).toBe("Suspended");
    expect(sandboxSuspended(suspended)).toBe(true);
  });

  it("reads controller and Pod status, owner references, and common policy fields", () => {
    const running = protoSandbox({
      phase: SandboxPhase.RUNNING,
      operatingMode: OperatingMode.RUNNING,
      status: { conditions: [{ type: "Ready", status: "True" }] },
      wakeOnSend: true,
      pod: create(SandboxPodSchema, {
        name: "test-sandbox-launcher",
        namespace: "agentplane-test",
        uid: `${UID}-pod`,
        ownerReferences: [
          {
            apiVersion: "kubevirt.io/v1",
            kind: "VirtualMachineInstance",
            uid: "vmi-uid",
            controller: true,
          },
        ],
        status: { phase: "Running", podIP: "10.0.0.1" },
      }),
    });
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
    [SandboxPhase.QUIESCING, { label: "Quiescing", kind: "pending" }],
    [SandboxPhase.TEARING_DOWN, { label: "Deleting", kind: "gone" }],
    [SandboxPhase.SUSPENDING, { label: "Suspending", kind: "pending" }],
    [SandboxPhase.SUSPENDED, { label: "Suspended", kind: "suspended" }],
  ] as const)("%s", (phase, summary) => {
    expect(sandboxSummary({ ...sandbox([{ type: "Ready", status: "False" }]), phase })).toEqual(summary);
  });

  it("does not report an unreconciled Sandbox as suspended", () => {
    expect(
      sandboxSummary({
        ...sandbox([{ type: "Ready", status: "True" }]),
        operatingMode: OperatingMode.UNSPECIFIED,
        phase: SandboxPhase.UNSPECIFIED,
        pod: undefined,
      })
    ).toEqual({
      label: "Status unknown",
      kind: "pending",
    });
    expect(
      sandboxReady({ ...sandbox([{ type: "Ready", status: "True" }]), operatingMode: OperatingMode.UNSPECIFIED })
    ).toBe(false);
  });
});

describe("a suspended Sandbox says why it is suspended", () => {
  const suspended: EnvironmentDescriptor = {
    ...sandbox([]),
    operatingMode: OperatingMode.SUSPENDED,
    phase: SandboxPhase.SUSPENDED,
    pod: undefined,
  };

  it("names an idle suspension and the time that triggered it", () => {
    const idle: EnvironmentDescriptor = {
      ...suspended,
      suspendedBy: "Idle",
      idleSuspendAfter: create(DurationSchema, { seconds: 5400n }),
    };
    expect(sandboxSummary(idle)).toEqual({ label: "Suspended (idle)", kind: "suspended" });
    expect(sandboxStatusDetail(idle)).toContain("Suspended because its runner was idle for 1 hour 30 minutes");
  });

  it("names a suspension a requester made", () => {
    const requested: EnvironmentDescriptor = { ...suspended, suspendedBy: "Requested" };
    expect(sandboxSummary(requested)).toEqual({ label: "Suspended (on request)", kind: "suspended" });
    expect(sandboxStatusDetail(requested)).toContain("Suspended on request");
  });

  it("names it while the Sandbox is still suspending, and says nothing before the controller reports", () => {
    expect(sandboxSummary({ ...suspended, phase: SandboxPhase.SUSPENDING, suspendedBy: "Idle" }).label).toBe(
      "Suspending (idle)"
    );
    expect(sandboxSummary(suspended).label).toBe("Suspended");
  });
});
