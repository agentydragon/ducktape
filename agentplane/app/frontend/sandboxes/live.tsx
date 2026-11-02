import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import {
  WatchEnvironmentsResponseSchema,
  type EnvironmentDescriptor,
  type TargetHandle,
} from "../../../sandboxes/environments/environment_service_pb";
import { followWatch } from "../watch_stream";
import { type StreamConnection } from "../stream_reader";
import { useStreamStatus } from "../stream_status";

import { type Live, type WatchHealth } from "../live";

import type { ActionPolicyUnavailable, ActionPolicyView } from "../actions/client";
import type { BindingView, Decision } from "./client";

export type SandboxesSnapshot = { sandboxes: EnvironmentDescriptor[]; watch: WatchHealth };
export type SandboxSnapshot = {
  sandbox_uid: string | null;
  bindings: BindingView[];
  action_policy: ActionPolicyView | ActionPolicyUnavailable | null;
  watch: WatchHealth;
};
export type DecisionsSnapshot = { watching: boolean; decisions: Decision[] | { detail: string } };

export function liveSandboxesUrl(): string {
  return "/upstream/environments/v1/environments:watch";
}

export function liveSandboxUrl(target: TargetHandle): string {
  return `/live/sandboxes/${encodeURIComponent(target.name)}?sandbox_uid=${encodeURIComponent(target.uid)}`;
}

export function liveEgressDecisionsUrl(target: TargetHandle): string {
  return `/live/sandboxes/${encodeURIComponent(target.name)}/egress/decisions?sandbox_uid=${encodeURIComponent(target.uid)}`;
}

const SandboxesLiveContext = createContext<Live<SandboxesSnapshot> | null>(null);

/** One caller-scoped EnvironmentService stream shared by all inventory consumers. */
export function SandboxesLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const [snapshot, setSnapshot] = useState<SandboxesSnapshot | null>(null);
  const [ready, setReady] = useState(false);
  const [connection, setConnection] = useState<StreamConnection>({ phase: "connecting", since: Date.now() });
  useEffect(() => {
    const key = (namespace: string, name: string): string => `${namespace}\0${name}`;
    let pending: Map<string, EnvironmentDescriptor> | null = new Map();
    let current = new Map<string, EnvironmentDescriptor>();
    const publish = (): void => {
      const sandboxes = Array.from(current.values()).sort(
        (a, b) => a.target!.namespace.localeCompare(b.target!.namespace) || a.target!.name.localeCompare(b.target!.name)
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
            for (const sandbox of frame.frame.value.environments) {
              const target = sandbox.target;
              if (!target?.namespace || !target.name || !target.uid)
                throw new Error("Environment missing target identity");
              pending.set(key(target.namespace, target.name), sandbox);
            }
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
            const target = sandbox.target;
            if (!target?.namespace || !target.name || !target.uid)
              throw new Error("Environment missing target identity");
            current.set(key(target.namespace, target.name), sandbox);
            publish();
            break;
          }
          case "removed": {
            if (pending !== null) throw new Error("Inventory removal before snapshot completion");
            const removed = frame.frame.value;
            const itemKey = key(removed.namespace, removed.name);
            if (current.get(itemKey)?.target?.uid === removed.uid) {
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
