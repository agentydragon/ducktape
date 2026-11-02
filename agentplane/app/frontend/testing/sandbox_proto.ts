import type { SandboxView } from "../schema";

/** ProtoJSON for the EnvironmentService inventory from a presentation fixture. */
export function sandboxProto(row: SandboxView): Record<string, unknown> {
  const phases: Record<string, string> = {
    Unspecified: "SANDBOX_PHASE_UNSPECIFIED",
    Creating: "SANDBOX_PHASE_CREATING",
    Running: "SANDBOX_PHASE_RUNNING",
    Suspending: "SANDBOX_PHASE_SUSPENDING",
    Suspended: "SANDBOX_PHASE_SUSPENDED",
    Quiescing: "SANDBOX_PHASE_QUIESCING",
    "Tearing down": "SANDBOX_PHASE_TEARING_DOWN",
  };
  const pod = row.pod;
  return {
    name: row.name,
    uid: row.uid,
    namespace: row.namespace,
    createdAt: row.created_at,
    operatingMode: row.operating_mode ?? undefined,
    status: row.status ?? undefined,
    serviceAccount: row.service_account,
    binding: row.binding ?? undefined,
    preset: row.preset ?? undefined,
    wakeOnSend: row.wake_on_send,
    idleSuspendAfter: row.idle_suspend_after_seconds == null ? undefined : `${row.idle_suspend_after_seconds}s`,
    idleSuspendAt: row.idle_suspend_at ?? undefined,
    suspendedBy: row.suspended_by ?? undefined,
    phase: phases[row.phase],
    autosuspendBlockers: row.autosuspend_blockers.map((blocker) => ({
      reason: blocker.reason,
      sessionId: blocker.session_id ?? undefined,
    })),
    pod: pod
      ? {
          name: pod.name,
          uid: pod.uid,
          namespace: pod.namespace,
          deleting: pod.deleting,
          nodeName: pod.node_name ?? undefined,
          status: pod.status ?? undefined,
          ownerReferences: pod.owner_references.map((ref) => ({
            apiVersion: ref.api_version,
            kind: ref.kind,
            name: ref.name,
            uid: ref.uid,
            controller: ref.controller,
          })),
        }
      : undefined,
  };
}
