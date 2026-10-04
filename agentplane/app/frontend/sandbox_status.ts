/** Presentation of Kubernetes' raw Sandbox and Pod status objects. */
import type { SandboxView } from "./client";
import type { SandboxStatusKind } from "./status_mark";

type RawObject = Record<string, unknown>;

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

export function podPhase(sandbox: SandboxView): string | null {
  return rawString(sandbox.pod?.status?.phase);
}

export function podIp(sandbox: SandboxView): string | null {
  return rawString(sandbox.pod?.status?.podIP);
}

/** Match the service's availability facts, including the Pod's controller UID. */
export function sandboxReady(sandbox: SandboxView | null | undefined): boolean {
  if (
    !sandbox ||
    sandbox.deleting ||
    sandbox.operating_mode !== "Running" ||
    sandbox.launch_grants_pending ||
    !sandbox.kubernetes_grants_ready ||
    sandbox.kubernetes_grant_error ||
    !sandbox.pod ||
    sandbox.pod.name !== sandbox.name ||
    sandbox.pod.namespace !== sandbox.namespace ||
    !sandbox.pod.uid ||
    sandbox.pod.deleting
  )
    return false;
  const controllers = sandbox.pod.owner_references.filter((owner) => owner.controller);
  if (controllers.length !== 1) return false;
  const owner = controllers[0];
  return (
    owner.api_version === "agents.x-k8s.io/v1beta1" &&
    owner.kind === "Sandbox" &&
    owner.name === sandbox.name &&
    owner.uid === sandbox.uid &&
    podPhase(sandbox) === "Running" &&
    podIp(sandbox) !== null &&
    rawConditions(sandbox.pod.status).some((condition) => condition.type === "Ready" && condition.status === "True")
  );
}

export function sandboxSummary(sandbox: SandboxView): { label: string; kind: SandboxStatusKind } {
  if (sandbox.deleting) return { label: "Deleting", kind: "gone" };
  if (sandbox.operating_mode === "Suspended") return { label: "Suspended", kind: "suspended" };
  if (sandbox.kubernetes_grant_error) return { label: "Grant error", kind: "failed" };
  if (sandbox.launch_grants_pending || !sandbox.kubernetes_grants_ready)
    return { label: "Applying grants", kind: "pending" };
  if (!sandbox.pod) return { label: "No Pod", kind: "pending" };
  if (sandbox.pod.deleting) return { label: "Pod deleting", kind: "gone" };
  if (sandboxReady(sandbox)) return { label: "Pod ready", kind: "ready" };
  const phase = podPhase(sandbox);
  if (phase === "Failed") return { label: "Pod Failed", kind: "failed" };
  if (phase === "Succeeded") return { label: "Pod Succeeded", kind: "gone" };
  return { label: phase ? `Pod ${phase} · not ready` : "Pod status unknown", kind: "pending" };
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

export function sandboxStatusDetail(sandbox: SandboxView): string {
  const lines = [`Sandbox: ${sandboxSummary(sandbox).label}`];
  lines.push(...rawConditions(sandbox.status).map((condition) => `Sandbox condition · ${conditionLine(condition)}`));
  if (sandbox.launch_grants_pending) lines.push("Launch grants pending");
  if (!sandbox.kubernetes_grants_ready) lines.push("Kubernetes grants are still being applied");
  if (sandbox.kubernetes_grant_error) lines.push(`Kubernetes grant error: ${sandbox.kubernetes_grant_error}`);
  if (sandbox.pod) {
    const status = sandbox.pod.status;
    lines.push(`Pod: ${podPhase(sandbox) ?? "phase unknown"}${sandbox.pod.deleting ? " · deleting" : ""}`);
    const controllers = sandbox.pod.owner_references.filter((owner) => owner.controller);
    if (controllers.length !== 1 || controllers[0]?.uid !== sandbox.uid) {
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
