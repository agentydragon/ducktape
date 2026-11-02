import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import { SandboxSchema, type Sandbox } from "../../../proto/sandboxes_pb";
import { WatchEnvironmentsResponseSchema } from "../../../sandboxes/environments/environment_service_pb";
import { followWatch } from "../watch_stream";
import { type StreamConnection } from "../stream_reader";
import { useStreamStatus } from "../stream_status";

import { type Live, type WatchHealth } from "../live";

import type { ActionPolicyUnavailable, ActionPolicyView } from "../actions/client";
import type { SandboxView, ThreadView } from "../schema";
import type { BindingView, Decision } from "./client";

export type SandboxesSnapshot = { sandboxes: Sandbox[]; watch: WatchHealth };
export type SandboxSnapshot = {
  sandbox: SandboxView | null;
  bindings: BindingView[];
  action_policy: ActionPolicyView | ActionPolicyUnavailable | null;
  threads: ThreadView[] | { kind: "unavailable"; code: string };
  updates_connected: boolean;
  watch: WatchHealth;
};
export type DecisionsSnapshot = { watching: boolean; decisions: Decision[] | { detail: string } };

export function liveSandboxesUrl(): string {
  return "/upstream/environments/v1/environments:watch";
}

export function liveSandboxUrl(name: string, includeArchived: boolean): string {
  return `/live/sandboxes/${encodeURIComponent(name)}?include_archived=${includeArchived}`;
}

export function liveEgressDecisionsUrl(name: string): string {
  return `/live/sandboxes/${encodeURIComponent(name)}/egress/decisions`;
}

const SandboxesLiveContext = createContext<Live<SandboxesSnapshot> | null>(null);

// The inventory's wire type is the shared Sandbox proto. The remaining detail and mutation
// screens use the app's presentation view; this projection keeps that view at the UI boundary.
export function sandboxView(value: Sandbox): SandboxView {
  const encoded = toJson(SandboxSchema, value, { useProtoFieldName: true, alwaysEmitImplicit: true });
  if (typeof encoded !== "object" || encoded === null || Array.isArray(encoded)) throw new Error("Invalid Sandbox");
  const phaseNames: Record<string, string> = {
    SANDBOX_PHASE_UNSPECIFIED: "Unspecified",
    SANDBOX_PHASE_CREATING: "Creating",
    SANDBOX_PHASE_RUNNING: "Running",
    SANDBOX_PHASE_SUSPENDING: "Suspending",
    SANDBOX_PHASE_SUSPENDED: "Suspended",
    SANDBOX_PHASE_QUIESCING: "Quiescing",
    SANDBOX_PHASE_TEARING_DOWN: "Tearing down",
  };
  const phase = phaseNames[String(encoded.phase)];
  if (!phase) throw new Error(`Unknown Sandbox phase ${encoded.phase}`);
  const duration = encoded.idle_suspend_after;
  const idle = typeof duration === "string" ? Number(duration.replace(/s$/, "")) : null;
  function object(value: unknown): Record<string, unknown> | null {
    return value !== null && typeof value === "object" && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value))
      : null;
  }
  const pod = value.pod;
  const podJson = object(encoded.pod);
  return {
    name: value.name,
    uid: value.uid,
    namespace: value.namespace,
    created_at: typeof encoded.created_at === "string" ? encoded.created_at : "0001-01-01T00:00:00Z",
    operating_mode: value.operatingMode ?? null,
    status: object(encoded.status) ?? {},
    service_account: { name: value.serviceAccount?.name ?? "", namespace: value.serviceAccount?.namespace ?? "" },
    binding: value.binding ? { bootstrap: value.binding.bootstrap } : null,
    preset: value.preset ?? null,
    wake_on_send: value.wakeOnSend,
    idle_suspend_after_seconds: idle,
    idle_suspend_at: typeof encoded.idle_suspend_at === "string" ? encoded.idle_suspend_at : null,
    suspended_by: value.suspendedBy ?? null,
    autosuspend_blockers: value.autosuspendBlockers.map((blocker) => {
      const reason = blocker.reason;
      if (
        reason !== "ActiveTurn" &&
        reason !== "PendingCommand" &&
        reason !== "Setup" &&
        reason !== "Activation" &&
        reason !== "Bootstrap" &&
        reason !== "Admission" &&
        reason !== "ExecutorRelay"
      ) {
        throw new Error(`Unknown autosuspend blocker ${reason}`);
      }
      return { reason, session_id: blocker.sessionId ?? null };
    }),
    phase,
    pod: pod
      ? {
          name: pod.name,
          namespace: pod.namespace,
          uid: pod.uid,
          deleting: pod.deleting,
          node_name: pod.nodeName ?? null,
          owner_references: pod.ownerReferences.map((owner) => ({
            api_version: owner.apiVersion,
            kind: owner.kind,
            name: owner.name,
            uid: owner.uid,
            controller: owner.controller,
          })),
          status: object(podJson?.status) ?? {},
        }
      : null,
  };
}

/** One caller-scoped EnvironmentService stream shared by all inventory consumers. */
export function SandboxesLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const [snapshot, setSnapshot] = useState<SandboxesSnapshot | null>(null);
  const [ready, setReady] = useState(false);
  const [connection, setConnection] = useState<StreamConnection>({ phase: "connecting", since: Date.now() });
  useEffect(() => {
    const key = (namespace: string, name: string): string => `${namespace}\0${name}`;
    let pending: Map<string, Sandbox> | null = new Map();
    let current = new Map<string, Sandbox>();
    const publish = (): void => {
      const sandboxes = Array.from(current.values()).sort(
        (a, b) => a.namespace.localeCompare(b.namespace) || a.name.localeCompare(b.name)
      );
      setSnapshot({ sandboxes, watch: { healthy: true, failing: {} } });
    };
    return followWatch(liveSandboxesUrl(), {
      onOpen: () => {
        pending = new Map();
        setReady(false);
      },
      onFrame: (result) => {
        const frame = fromJson(WatchEnvironmentsResponseSchema, result as JsonValue);
        switch (frame.frame.case) {
          case "snapshot":
            if (pending === null) throw new Error("Unexpected inventory snapshot chunk");
            for (const sandbox of frame.frame.value.sandboxes)
              pending.set(key(sandbox.namespace, sandbox.name), sandbox);
            break;
          case "snapshotComplete":
            if (pending === null) throw new Error("Unexpected inventory snapshot completion");
            current = pending;
            pending = null;
            publish();
            setReady(true);
            break;
          case "upsert": {
            if (pending !== null) throw new Error("Inventory update before snapshot completion");
            const sandbox = frame.frame.value;
            current.set(key(sandbox.namespace, sandbox.name), sandbox);
            publish();
            break;
          }
          case "removed": {
            if (pending !== null) throw new Error("Inventory removal before snapshot completion");
            const removed = frame.frame.value;
            const itemKey = key(removed.namespace, removed.name);
            if (current.get(itemKey)?.uid === removed.uid) {
              current.delete(itemKey);
              publish();
            }
            break;
          }
          default:
            throw new Error("EnvironmentService returned an empty watch frame");
        }
      },
      onConnection: (next) => {
        setConnection(next);
        if (next.phase !== "live") setReady(false);
      },
    });
  }, []);
  const stalled = !ready
    ? connection.phase === "reconnecting"
      ? (connection.lastError ?? "Inventory watch disconnected")
      : "Waiting for inventory snapshot"
    : null;
  const stream = useStreamStatus("Sandboxes", connection, stalled);
  const live: Live<SandboxesSnapshot> = { snapshot, health: snapshot?.watch ?? null, stream };
  return <SandboxesLiveContext.Provider value={live}>{children}</SandboxesLiveContext.Provider>;
}

/** Whether the inventory stream is current and the server's watch behind it healthy, so its Sandboxes are live. */
export function sandboxInventoryFresh(live: Live<SandboxesSnapshot>): boolean {
  return live.stream.standing === "current" && live.health?.healthy === true;
}

export function useRequiredSandboxesLive(): Live<SandboxesSnapshot> {
  const live = useContext(SandboxesLiveContext);
  if (!live) throw new Error("Sandbox status requires SandboxesLiveProvider");
  return live;
}
