import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  SandboxPhase,
  SandboxPodSchema,
  EnvironmentDescriptorSchema,
  OperatingMode,
  ReadinessSchema,
  ReadinessState,
  type EnvironmentDescriptor,
} from "../../../runner_gateway/environments/environment_service_pb";

/** A fresh Sandbox with a fixed creation time, identity, and execution account. */
export function testSandbox(name: string, uid: string, namespace = "agentplane-test"): EnvironmentDescriptor {
  return create(EnvironmentDescriptorSchema, {
    target: { name, uid, namespace },
    createdAt: timestampFromDate(new Date("2026-01-01T00:00:00Z")),
    executionAccount: { namespace, name, uid: `${uid}-account` },
    readiness: { state: ReadinessState.NOT_READY, reason: "ReadyConditionUnavailable" },
  });
}

/** A running Sandbox with a ready Pod; callers can change fields for their scenario. */
export function readySandbox(name: string, uid: string): EnvironmentDescriptor {
  const sandbox = testSandbox(name, uid);
  sandbox.operatingMode = OperatingMode.RUNNING;
  sandbox.readiness = create(ReadinessSchema, { state: ReadinessState.READY });
  sandbox.phase = SandboxPhase.RUNNING;
  sandbox.status = { conditions: [{ type: "Ready", status: "True" }] };
  sandbox.pod = create(SandboxPodSchema, {
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
  sandbox.operatingMode = OperatingMode.SUSPENDED;
  sandbox.readiness = create(ReadinessSchema, { state: ReadinessState.NOT_READY, reason: "Suspended" });
  sandbox.phase = SandboxPhase.SUSPENDED;
  sandbox.status = {};
  return sandbox;
}
