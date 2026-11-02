import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import type { Sandbox } from "../../../proto/sandboxes_pb";
import { WatchEnvironmentsResponseSchema } from "../../../runner_gateway/environments/environment_service_pb";
import { followWatch } from "../watch_stream";
import { type StreamConnection } from "../stream_reader";
import { useStreamStatus } from "../stream_status";

import { type Live, type WatchHealth } from "../live";

import type { ActionPolicyUnavailable, ActionPolicyView } from "../actions/client";
import type { SandboxView } from "../schema";
import type { BindingView, Decision } from "./client";

export type SandboxesSnapshot = { sandboxes: Sandbox[]; watch: WatchHealth };
export type SandboxSnapshot = {
  sandbox: SandboxView | null;
  bindings: BindingView[];
  action_policy: ActionPolicyView | ActionPolicyUnavailable | null;
  watch: WatchHealth;
};
export type DecisionsSnapshot = { watching: boolean; decisions: Decision[] | { detail: string } };

export function liveSandboxesUrl(): string {
  return "/upstream/environments/v1/environments:watch";
}

export function liveSandboxUrl(name: string, uid: string): string {
  return `/live/sandboxes/${encodeURIComponent(name)}?sandbox_uid=${encodeURIComponent(uid)}`;
}

export function liveEgressDecisionsUrl(name: string): string {
  return `/live/sandboxes/${encodeURIComponent(name)}/egress/decisions`;
}

const SandboxesLiveContext = createContext<Live<SandboxesSnapshot> | null>(null);

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
