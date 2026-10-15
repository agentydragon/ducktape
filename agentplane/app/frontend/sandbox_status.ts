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

/** The Sandbox Controller's reported running mode and `Ready` condition; no caller reads into the incarnation. */
export function sandboxReady(sandbox: SandboxView | null | undefined): boolean {
  return (
    sandbox?.operating_mode === "Running" &&
    rawConditions(rawObject(sandbox.status)).some((c) => c.type === "Ready" && c.status === "True")
  );
}

export function sandboxSummary(sandbox: SandboxView): { label: string; kind: SandboxStatusKind } {
  if (sandbox.phase === "Unspecified") return { label: "Status unknown", kind: "pending" };
  if (sandbox.phase === "Tearing down") return { label: "Deleting", kind: "gone" };
  if (sandbox.phase === "Quiescing") return { label: "Quiescing", kind: "pending" };
  if (sandbox.phase === "Suspending") return { label: "Suspending", kind: "pending" };
  if (sandbox.phase === "Suspended") return { label: "Suspended", kind: "suspended" };
  // Neutral nouns: a VM Sandbox has no Pod of its own to name here. The tooltip carries the Pod's phase.
  if (!sandbox.pod) return { label: "Not started", kind: "pending" };
  if (sandbox.pod.deleting) return { label: "Shutting down", kind: "gone" };
  if (sandboxReady(sandbox)) return { label: "Ready", kind: "ready" };
  const phase = podPhase(sandbox);
  if (phase === "Failed") return { label: "Failed", kind: "failed" };
  if (phase === "Succeeded") return { label: "Exited", kind: "gone" };
  return { label: phase === "Running" ? "Not ready" : phase ? "Starting" : "Status unknown", kind: "pending" };
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
