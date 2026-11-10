import { clone, create } from "@bufbuild/protobuf";
import { DurationSchema } from "@bufbuild/protobuf/wkt";
import { describe, expect, it } from "vitest";

import {
  SandboxPhase,
  SandboxPodSchema,
  EnvironmentDescriptorSchema,
  type EnvironmentActions,
  type EnvironmentConfiguration,
  type EnvironmentObservedState,
  type KubernetesDiagnostics,
  OperatingMode,
  ReadinessSchema,
  ReadinessState,
  type EnvironmentDescriptor,
} from "../../environments/v1/environment_service_pb";

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
} from "./sandbox_status";

const UID = "00000000-0000-4000-8000-000000000001";

type DescriptorChanges = Omit<
  Partial<EnvironmentDescriptor>,
  "configuration" | "observedState" | "actions" | "kubernetesDiagnostics"
> & {
  configuration?: Partial<EnvironmentConfiguration>;
  observedState?: Partial<EnvironmentObservedState>;
  actions?: Partial<EnvironmentActions>;
  kubernetesDiagnostics?: Partial<KubernetesDiagnostics>;
};

function updateSandbox(base: EnvironmentDescriptor, changes: DescriptorChanges): EnvironmentDescriptor {
  const updated = clone(EnvironmentDescriptorSchema, base);
  const { configuration, observedState, actions, kubernetesDiagnostics, ...topLevel } = changes;
  Object.assign(updated, topLevel);
  if (configuration) Object.assign(updated.configuration!, configuration);
  if (observedState) Object.assign(updated.observedState!, observedState);
  if (actions) Object.assign(updated.actions!, actions);
  if (kubernetesDiagnostics) Object.assign(updated.kubernetesDiagnostics!, kubernetesDiagnostics);
  return updated;
}

function sandbox(conditions: { type: string; status: string }[]): EnvironmentDescriptor {
  const value = testSandbox("test-sandbox", UID);
  value.observedState!.operatingMode = OperatingMode.RUNNING;
  value.observedState!.phase = SandboxPhase.RUNNING;
  value.kubernetesDiagnostics!.sandboxStatus = { conditions };
  value.kubernetesDiagnostics!.pod = create(SandboxPodSchema, {
    name: "test-sandbox-launcher",
    namespace: "agentplane-test",
    uid: `${UID}-pod`,
    nodeName: "test-node",
    status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
  });
  return value;
}

function protoSandbox(overrides: DescriptorChanges = {}): EnvironmentDescriptor {
  return updateSandbox(testSandbox("test-sandbox", UID), overrides);
}

describe("a Sandbox exposes the VM console only for a VMI-backed incarnation", () => {
  it("distinguishes its own Pod from a KubeVirt launcher Pod", () => {
    const podSandbox = sandbox([{ type: "Ready", status: "True" }]);
    const vmSandbox = updateSandbox(podSandbox, {
      kubernetesDiagnostics: {
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
      },
    });
    expect(hasVmConsole(podSandbox)).toBe(false);
    expect(hasVmConsole(vmSandbox)).toBe(true);
    expect(hasVmConsole(updateSandbox(podSandbox, { kubernetesDiagnostics: { pod: undefined } }))).toBe(false);
  });
});

describe("environment readiness uses the service descriptor", () => {
  it("uses canonical readiness independently of raw diagnostic conditions", () => {
    const ready = sandbox([{ type: "Ready", status: "False" }]);
    ready.observedState!.readiness = create(ReadinessSchema, { state: ReadinessState.READY });
    expect(sandboxReady(ready)).toBe(true);
    expect(sandboxSummary(ready).kind).toBe("ready");
  });

  it.each([ReadinessState.UNSPECIFIED, ReadinessState.NOT_READY])(
    "is not ready for state %s, even with a Ready Pod and condition",
    (state) => {
      const notReady = sandbox([{ type: "Ready", status: "True" }]);
      notReady.observedState!.readiness = create(ReadinessSchema, { state });
      expect(sandboxReady(notReady)).toBe(false);
    }
  );
});

describe("the shared status helpers accept generated environment descriptors", () => {
  it("normalizes generated phases and keeps them separate from operating mode", () => {
    const unreconciled = protoSandbox({
      observedState: { phase: SandboxPhase.UNSPECIFIED, operatingMode: OperatingMode.RUNNING },
    });
    expect(sandboxPhase(unreconciled)).toBe("Unspecified");
    expect(sandboxOperatingMode(unreconciled)).toBe("Running");
    expect(sandboxSummary(unreconciled)).toEqual({ label: "Status unknown", kind: "pending" });

    const suspended = protoSandbox({
      observedState: { phase: SandboxPhase.SUSPENDED, operatingMode: OperatingMode.SUSPENDED },
    });
    expect(sandboxPhase(suspended)).toBe("Suspended");
    expect(sandboxSuspended(suspended)).toBe(true);
  });

  it("reads controller and Pod status, owner references, and common policy fields", () => {
    const running = protoSandbox({
      observedState: {
        phase: SandboxPhase.RUNNING,
        operatingMode: OperatingMode.RUNNING,
        readiness: create(ReadinessSchema, { state: ReadinessState.READY }),
      },
      configuration: { wakeOnSend: true },
      kubernetesDiagnostics: {
        sandboxStatus: { conditions: [{ type: "Ready", status: "True" }] },
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
      },
    });
    expect(sandboxReady(running)).toBe(true);
    expect(sandboxSummary(running)).toEqual({ label: "Ready", kind: "ready" });
    expect(hasVmConsole(running)).toBe(true);
    expect(podPhase(running)).toBe("Running");
    expect(podIp(running)).toBe("10.0.0.1");
  });

  it("preserves duration presence, zero, and fractional seconds", () => {
    expect(sandboxIdleSuspendAfterSeconds(protoSandbox())).toBeNull();
    expect(
      sandboxIdleSuspendAfterSeconds(
        protoSandbox({ configuration: { idleSuspendAfter: create(DurationSchema, { seconds: 0n }) } })
      )
    ).toBe(0);
    expect(
      sandboxIdleSuspendAfterSeconds(
        protoSandbox({
          configuration: { idleSuspendAfter: create(DurationSchema, { seconds: 90n, nanos: 500_000_000 }) },
        })
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
    expect(
      sandboxSummary(updateSandbox(sandbox([{ type: "Ready", status: "False" }]), { observedState: { phase } }))
    ).toEqual(summary);
  });

  it("does not report an unreconciled Sandbox as suspended", () => {
    const unknown = updateSandbox(sandbox([{ type: "Ready", status: "True" }]), {
      observedState: { operatingMode: OperatingMode.UNSPECIFIED, phase: SandboxPhase.UNSPECIFIED },
      kubernetesDiagnostics: { pod: undefined },
    });
    expect(sandboxSummary(unknown)).toEqual({
      label: "Status unknown",
      kind: "pending",
    });
    expect(
      sandboxReady(
        updateSandbox(sandbox([{ type: "Ready", status: "True" }]), {
          observedState: { operatingMode: OperatingMode.UNSPECIFIED },
        })
      )
    ).toBe(false);
  });
});

describe("a suspended Sandbox says why it is suspended", () => {
  const suspended = updateSandbox(sandbox([]), {
    observedState: { operatingMode: OperatingMode.SUSPENDED, phase: SandboxPhase.SUSPENDED },
    kubernetesDiagnostics: { pod: undefined },
  });

  it("names an idle suspension and the time that triggered it", () => {
    const idle = updateSandbox(suspended, {
      observedState: { ...suspended.observedState, suspendedBy: "Idle" },
      configuration: { idleSuspendAfter: create(DurationSchema, { seconds: 5400n }) },
    });
    expect(sandboxSummary(idle)).toEqual({ label: "Suspended (idle)", kind: "suspended" });
    expect(sandboxStatusDetail(idle)).toContain("Suspended because its runner was idle for 1 hour 30 minutes");
  });

  it("names a suspension a requester made", () => {
    const requested = updateSandbox(suspended, { observedState: { suspendedBy: "Requested" } });
    expect(sandboxSummary(requested)).toEqual({ label: "Suspended (on request)", kind: "suspended" });
    expect(sandboxStatusDetail(requested)).toContain("Suspended on request");
  });

  it("names it while the Sandbox is still suspending, and says nothing before the controller reports", () => {
    expect(
      sandboxSummary(
        updateSandbox(suspended, { observedState: { phase: SandboxPhase.SUSPENDING, suspendedBy: "Idle" } })
      ).label
    ).toBe("Suspending (idle)");
    expect(sandboxSummary(suspended).label).toBe("Suspended");
  });
});
