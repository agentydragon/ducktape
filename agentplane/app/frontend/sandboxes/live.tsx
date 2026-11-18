import { fromJson } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import { type EnvironmentTarget } from "../../../proto/identity_pb";
import {
  WatchEnvironmentsResponseSchema,
  WatchEnvironmentResponseSchema,
  type EnvironmentChunk,
  type EnvironmentDescriptor,
} from "../../../environments/v1/environment_service_pb";
import { WatchCollection } from "../transport/watch_collection";
import { followWatch } from "../transport/watch_stream";
import { type StreamConnection } from "../transport/stream_reader";
import { useStreamStatus } from "../transport/stream_status";

import { type Live } from "../transport/live";

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
    const collection = new WatchCollection<EnvironmentDescriptor, EnvironmentTarget>(
      "Environment inventory",
      (sandbox) => sandbox.target,
      (target) => (target.namespace && target.name ? `${target.namespace}\0${target.name}` : undefined)
    );
    return followWatch(liveSandboxesUrl(), {
      onOpen: () => {
        collection.beginSnapshot();
        setReady(false);
      },
      onFrame: (result) => {
        const { frame } = fromJson(WatchEnvironmentsResponseSchema, result);
        const sandboxes = collection.apply<EnvironmentChunk>(frame, (chunk) => chunk.environments);
        if (sandboxes === null) return;
        sandboxes.sort(
          (a, b) =>
            a.target!.namespace.localeCompare(b.target!.namespace) || a.target!.name.localeCompare(b.target!.name)
        );
        setSnapshot(sandboxes);
        if (frame.case === "snapshotComplete") setReady(true);
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
          const environment = fromJson(WatchEnvironmentResponseSchema, result).environment;
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
