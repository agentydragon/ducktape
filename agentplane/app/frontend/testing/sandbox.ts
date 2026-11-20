import { clone, create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  SandboxPhase,
  SandboxPodSchema,
  EnvironmentDescriptorSchema,
  OperatingMode,
  ReadinessSchema,
  ReadinessState,
  type EnvironmentActions,
  type EnvironmentConfiguration,
  type EnvironmentDescriptor,
  type EnvironmentObservedState,
  type KubernetesDiagnostics,
} from "../../../environments/v1/environment_service_pb";

/** A fresh Sandbox with a fixed creation time, identity, and execution account. */
export function testSandbox(name: string, uid: string, namespace = "agentplane-test"): EnvironmentDescriptor {
  return create(EnvironmentDescriptorSchema, {
    target: { name, uid, namespace },
    executionAccount: { account: { namespace, name }, uid: `${uid}-account` },
    configuration: {},
    observedState: {
      createdAt: timestampFromDate(new Date("2026-01-01T00:00:00Z")),
      readiness: { state: ReadinessState.NOT_READY, reason: "ReadyConditionUnavailable" },
    },
    kubernetesDiagnostics: {},
  });
}

/** A running Sandbox with a ready Pod; callers can change fields for their scenario. */
export function readySandbox(name: string, uid: string): EnvironmentDescriptor {
  const sandbox = testSandbox(name, uid);
  sandbox.observedState!.operatingMode = OperatingMode.RUNNING;
  sandbox.observedState!.readiness = create(ReadinessSchema, { state: ReadinessState.READY });
  sandbox.observedState!.phase = SandboxPhase.RUNNING;
  sandbox.kubernetesDiagnostics!.sandboxStatus = { conditions: [{ type: "Ready", status: "True" }] };
  sandbox.kubernetesDiagnostics!.pod = create(SandboxPodSchema, {
    name,
    namespace: sandbox.target!.namespace,
    uid: `${uid}-pod`,
    nodeName: "test-node",
    ownerReferences: [
      { apiVersion: "agentplane.allegedly.works/v1alpha1", kind: "Sandbox", name, uid, controller: true },
    ],
    status: { phase: "Running", podIP: "10.0.0.1", conditions: [{ type: "Ready", status: "True" }] },
  });
  return sandbox;
}

/** A suspended Sandbox with no current Pod. */
export function suspendedSandbox(name: string, uid: string): EnvironmentDescriptor {
  const sandbox = testSandbox(name, uid);
  sandbox.observedState!.operatingMode = OperatingMode.SUSPENDED;
  sandbox.observedState!.readiness = create(ReadinessSchema, { state: ReadinessState.NOT_READY, reason: "Suspended" });
  sandbox.observedState!.phase = SandboxPhase.SUSPENDED;
  sandbox.kubernetesDiagnostics!.sandboxStatus = {};
  return sandbox;
}

/** Field changes for `changedSandbox`; a nested message takes a partial that merges into the existing one. */
export type SandboxChanges = Omit<
  Partial<EnvironmentDescriptor>,
  "configuration" | "observedState" | "actions" | "kubernetesDiagnostics"
> & {
  configuration?: Partial<EnvironmentConfiguration>;
  observedState?: Partial<EnvironmentObservedState>;
  actions?: Partial<EnvironmentActions>;
  kubernetesDiagnostics?: Partial<KubernetesDiagnostics>;
};

/** A copy of `base` with `changes` merged over it, one nested message at a time. */
export function changedSandbox(base: EnvironmentDescriptor, changes: SandboxChanges): EnvironmentDescriptor {
  const changed = clone(EnvironmentDescriptorSchema, base);
  const { configuration, observedState, actions, kubernetesDiagnostics, ...topLevel } = changes;
  Object.assign(changed, topLevel);
  if (configuration) Object.assign(changed.configuration!, configuration);
  if (observedState) Object.assign(changed.observedState!, observedState);
  if (actions) Object.assign(changed.actions!, actions);
  if (kubernetesDiagnostics) Object.assign(changed.kubernetesDiagnostics!, kubernetesDiagnostics);
  return changed;
}
