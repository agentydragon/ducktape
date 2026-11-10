import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import { type EnvironmentTarget } from "../../../proto/identity_pb";
import {
  WatchEnvironmentsResponseSchema,
  WatchEnvironmentResponseSchema,
  type EnvironmentDescriptor,
} from "../../../environments/v1/environment_service_pb";
import { followWatch } from "../watch_stream";
import { type StreamConnection } from "../stream_reader";
import { useStreamStatus } from "../stream_status";

import { type Live } from "../live";

export function liveSandboxesUrl(): string {
  return "/upstream/environments/v1/environments:watch";
}

const SandboxesLiveContext = createContext<Live<EnvironmentDescriptor[]> | null>(null);

/** One caller-scoped EnvironmentService stream shared by all inventory consumers. */
export function SandboxesLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const [snapshot, setSnapshot] = useState<EnvironmentDescriptor[] | null>(null);
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
      setSnapshot(sandboxes);
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
  const live: Live<EnvironmentDescriptor[]> = {
    snapshot,
    health: snapshot === null ? null : { healthy: true, failing: {} },
    stream,
  };
  return <SandboxesLiveContext.Provider value={live}>{children}</SandboxesLiveContext.Provider>;
}

/** Whether the inventory stream is current and the server's watch behind it healthy, so its Sandboxes are live. */
export function sandboxInventoryFresh(live: Live<EnvironmentDescriptor[]>): boolean {
  return live.stream.standing === "current" && live.health?.healthy === true;
}

export function useRequiredSandboxesLive(): Live<EnvironmentDescriptor[]> {
  const live = useContext(SandboxesLiveContext);
  if (!live) throw new Error("Sandbox status requires SandboxesLiveProvider");
  return live;
}

const DISABLED_ENVIRONMENT_CONNECTION: StreamConnection = { phase: "live", since: 0 };

/** Exact-target observation; a reconnect begins a fresh generated EnvironmentService snapshot. */
export function useEnvironmentWatch(target: EnvironmentTarget | undefined): {
  environment: EnvironmentDescriptor | null;
  stream: ReturnType<typeof useStreamStatus>;
} {
  const namespace = target?.namespace;
  const name = target?.name;
  const uid = target?.uid;
  const identity = namespace && name && uid ? `${namespace}\0${name}\0${uid}` : null;
  const [observed, setObserved] = useState<{ identity: string; environment: EnvironmentDescriptor } | null>(null);
  const [connection, setConnection] = useState<StreamConnection>({ phase: "connecting", since: Date.now() });
  useEffect(() => {
    if (!namespace || !name || !uid || identity === null) return;
    let active = true;
    setObserved(null);
    const stop = followWatch(
      `/upstream/environments/v1/environments/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}:watch?target.uid=${encodeURIComponent(uid)}`,
      {
        onOpen: () => {
          if (active) setObserved(null);
        },
        onFrame: (result) => {
          if (!active) return;
          const environment = fromJson(WatchEnvironmentResponseSchema, result as JsonValue).environment;
          if (
            environment?.target?.namespace !== namespace ||
            environment.target.name !== name ||
            environment.target.uid !== uid
          )
            throw new Error("EnvironmentService returned a different target");
          setObserved({ identity, environment });
        },
        onConnection: (next) => {
          if (active) setConnection(next);
        },
      }
    );
    return () => {
      active = false;
      stop();
    };
  }, [namespace, name, uid, identity]);
  const environment = observed?.identity === identity ? observed.environment : null;
  const stream = useStreamStatus(
    "Environment",
    identity === null ? DISABLED_ENVIRONMENT_CONNECTION : connection,
    identity !== null && environment === null ? "Waiting for environment snapshot" : null
  );
  return { environment, stream };
}
