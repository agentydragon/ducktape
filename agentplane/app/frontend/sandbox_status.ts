/** Presentation of Kubernetes' raw Sandbox and Pod status objects. */
import { durationMs } from "@bufbuild/protobuf/wkt";
import {
  SandboxPhase,
  OperatingMode,
  ReadinessState,
  type EnvironmentDescriptor,
} from "../../environments/v1/environment_service_pb";

import { durationLabel } from "./format_time";
import type { SandboxStatusKind } from "./status_mark";

type RawObject = Record<string, unknown>;

/** The controller's lifecycle phase, normalized without deriving it from the requested operating mode. */
export function sandboxPhase(sandbox: EnvironmentDescriptor): string {
  switch (sandbox.observedState?.phase) {
    case SandboxPhase.UNSPECIFIED:
      return "Unspecified";
    case SandboxPhase.CREATING:
      return "Creating";
    case SandboxPhase.RUNNING:
      return "Running";
    case SandboxPhase.SUSPENDING:
      return "Suspending";
    case SandboxPhase.SUSPENDED:
      return "Suspended";
    case SandboxPhase.QUIESCING:
      return "Quiescing";
    case SandboxPhase.TEARING_DOWN:
      return "Tearing down";
    default:
      return "Unspecified";
  }
}

export function sandboxOperatingMode(sandbox: EnvironmentDescriptor): string | null {
  switch (sandbox.observedState?.operatingMode) {
    case OperatingMode.RUNNING:
      return "Running";
    case OperatingMode.SUSPENDED:
      return "Suspended";
    default:
      return null;
  }
}

/** Converts a present protobuf Duration to seconds while keeping absent and zero durations distinct. */
export function sandboxIdleSuspendAfterSeconds(sandbox: EnvironmentDescriptor): number | null {
  const duration = sandbox.configuration?.idleSuspendAfter;
  return duration === undefined ? null : durationMs(duration) / 1000;
}

export interface RawCondition {
  type: string;
  status: string;
  reason: string | null;
  message: string | null;
  lastTransitionTime: string | null;
  lastProbeTime: string | null;
  observedGeneration: number | null;
}

export interface ContainerStatus {
  name: string;
  role: "init" | "container" | "ephemeral";
  state: string;
  reason: string | null;
  message: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  exitCode: number | null;
  ready: boolean;
  restartCount: number;
}

export function rawObject(value: unknown): RawObject | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as RawObject) : null;
}

export function rawString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function rawNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function rawConditions(status: RawObject | null | undefined): RawCondition[] {
  const values = status?.conditions;
  if (!Array.isArray(values)) return [];
  return values.flatMap((value) => {
    const row = rawObject(value);
    const type = rawString(row?.type);
    if (!row || !type) return [];
    return [
      {
        type,
        status: rawString(row.status) ?? "Unknown",
        reason: rawString(row.reason),
        message: rawString(row.message),
        lastTransitionTime: rawString(row.lastTransitionTime),
        lastProbeTime: rawString(row.lastProbeTime),
        observedGeneration: rawNumber(row.observedGeneration),
      },
    ];
  });
}

export function podPhase(sandbox: EnvironmentDescriptor): string | null {
  return rawString(rawObject(sandbox.kubernetesDiagnostics?.pod?.status)?.phase);
}

export function podIp(sandbox: EnvironmentDescriptor): string | null {
  return rawString(rawObject(sandbox.kubernetesDiagnostics?.pod?.status)?.podIP);
}

/** Whether the current Sandbox incarnation is the VMI-backed Pod whose serial console is available. */
export function hasVmConsole(sandbox: EnvironmentDescriptor | null): boolean {
  return (
    (sandbox ? sandbox.kubernetesDiagnostics?.pod?.ownerReferences : null)?.some(
      (owner) =>
        owner.controller && owner.kind === "VirtualMachineInstance" && owner.apiVersion.startsWith("kubevirt.io/")
    ) ?? false
  );
}

export function sandboxReady(sandbox: EnvironmentDescriptor | null | undefined): boolean {
  return (
    sandbox?.observedState?.operatingMode === OperatingMode.RUNNING &&
    sandbox.observedState?.readiness?.state === ReadinessState.READY
  );
}

export function sandboxSuspended(sandbox: EnvironmentDescriptor | null | undefined): boolean {
  return (
    sandbox !== null &&
    sandbox !== undefined &&
    sandboxPhase(sandbox) === "Suspended" &&
    sandboxOperatingMode(sandbox) === "Suspended"
  );
}

export function sandboxStarting(sandbox: EnvironmentDescriptor): boolean {
  return (
    sandboxOperatingMode(sandbox) === "Running" &&
    ["Creating", "Running"].includes(sandboxPhase(sandbox)) &&
    sandboxSummary(sandbox).kind === "pending"
  );
}

/** Why a Sandbox is suspended, as the views word it; null while it runs and until the controller reports. */
export function suspensionReason(sandbox: EnvironmentDescriptor): "idle" | "on request" | null {
  const suspendedBy = sandbox.observedState?.suspendedBy;
  switch (suspendedBy) {
    case "Idle":
      return "idle";
    case "Requested":
      return "on request";
    default:
      return null;
  }
}

export function sandboxSummary(sandbox: EnvironmentDescriptor): { label: string; kind: SandboxStatusKind } {
  const phase = sandboxPhase(sandbox);
  const pod = sandbox.kubernetesDiagnostics?.pod;
  const reason = suspensionReason(sandbox);
  const withReason = (label: string): string => (reason === null ? label : `${label} (${reason})`);
  if (phase === "Unspecified") return { label: "Status unknown", kind: "pending" };
  if (phase === "Tearing down") return { label: "Deleting", kind: "gone" };
  if (phase === "Quiescing") return { label: "Quiescing", kind: "pending" };
  if (phase === "Suspending") return { label: withReason("Suspending"), kind: "pending" };
  if (phase === "Suspended") return { label: withReason("Suspended"), kind: "suspended" };
  // Neutral nouns: a VM Sandbox has no Pod of its own to name here. The tooltip carries the Pod's phase.
  if (!pod) return { label: "Not started", kind: "pending" };
  if (pod.deleting) return { label: "Shutting down", kind: "gone" };
  if (sandboxReady(sandbox)) return { label: "Ready", kind: "ready" };
  const currentPodPhase = podPhase(sandbox);
  if (currentPodPhase === "Failed") return { label: "Failed", kind: "failed" };
  if (currentPodPhase === "Succeeded") return { label: "Exited", kind: "gone" };
  return {
    label: currentPodPhase === "Running" ? "Not ready" : currentPodPhase ? "Starting" : "Status unknown",
    kind: "pending",
  };
}

function containerState(
  value: RawObject
): Pick<ContainerStatus, "state" | "reason" | "message" | "startedAt" | "finishedAt" | "exitCode"> {
  const state = rawObject(value.state);
  for (const kind of ["running", "waiting", "terminated"] as const) {
    const details = rawObject(state?.[kind]);
    if (details)
      return {
        state: kind,
        reason: rawString(details.reason),
        message: rawString(details.message),
        startedAt: rawString(details.startedAt),
        finishedAt: rawString(details.finishedAt),
        exitCode: rawNumber(details.exitCode),
      };
  }
  return { state: "unknown", reason: null, message: null, startedAt: null, finishedAt: null, exitCode: null };
}

export function containerStatuses(status: RawObject | null | undefined): ContainerStatus[] {
  return (
    [
      ["initContainerStatuses", "init"],
      ["containerStatuses", "container"],
      ["ephemeralContainerStatuses", "ephemeral"],
    ] as const
  ).flatMap(([key, role]) => {
    const values = status?.[key];
    if (!Array.isArray(values)) return [];
    return values.flatMap((value) => {
      const row = rawObject(value);
      const name = rawString(row?.name);
      if (!row || !name) return [];
      return [
        {
          name,
          role,
          ...containerState(row),
          ready: row.ready === true,
          restartCount: rawNumber(row.restartCount) ?? 0,
        },
      ];
    });
  });
}

function conditionLine(condition: RawCondition): string {
  return [
    `${condition.type}=${condition.status}`,
    condition.reason,
    condition.message,
    condition.lastTransitionTime ? `changed ${condition.lastTransitionTime}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function sandboxStatusDetail(sandbox: EnvironmentDescriptor): string {
  const lines = [`Sandbox: ${sandboxSummary(sandbox).label}`];
  const reason = suspensionReason(sandbox);
  const idleSuspendAfterSeconds = sandboxIdleSuspendAfterSeconds(sandbox);
  const pod = sandbox.kubernetesDiagnostics?.pod;
  if (reason !== null) {
    lines.push(
      reason === "idle" && idleSuspendAfterSeconds
        ? `Suspended because its runner was idle for ${durationLabel(idleSuspendAfterSeconds)}`
        : `Suspended ${reason}`
    );
  }
  lines.push(
    ...rawConditions(rawObject(sandbox.kubernetesDiagnostics?.sandboxStatus)).map(
      (condition) => `Sandbox condition · ${conditionLine(condition)}`
    )
  );
  if (pod) {
    const status = pod.status;
    lines.push(`Pod: ${podPhase(sandbox) ?? "phase unknown"}${pod.deleting ? " · deleting" : ""}`);
    const controllers = pod.ownerReferences.filter((owner) => owner.controller);
    if (controllers.length !== 1 || controllers[0]?.uid !== sandbox.target?.uid) {
      lines.push("Pod controller identity does not match this Sandbox");
    }
    lines.push(...rawConditions(status).map((condition) => `Pod condition · ${conditionLine(condition)}`));
    lines.push(
      ...containerStatuses(status).map((container) =>
        [
          `${container.role} ${container.name}: ${container.state}`,
          container.reason,
          container.message,
          container.restartCount > 0 ? `${container.restartCount} restarts` : null,
        ]
          .filter(Boolean)
          .join(" · ")
      )
    );
  }
  return lines.join("\n");
}
