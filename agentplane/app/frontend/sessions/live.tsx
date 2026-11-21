import { fromJson } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { useMatch } from "react-router";

import { sessionDispatcherClient } from "../transport/session_dispatcher_client";
import type { Session } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";

import { displayableError } from "../transport/client";
import { type Live, type WatchHealth } from "../transport/live";
import { backoff, type StreamConnection } from "../transport/stream_reader";
import { useStreamStatus } from "../transport/stream_status";
import { followWatch } from "../transport/watch_stream";
import { WatchThreadMetadataResponseSchema } from "../../app_service_pb";
import {
  WatchSelectedSessionSummariesResponseSchema,
  type SessionSummary,
} from "../../../history_service/api/history/history_service_pb";

import { type ThreadView, listThreadMetadata, selectThreadWindow, type ThreadListing } from "./client";

/** The Thread window. Sandboxes come from the EnvironmentService inventory watch (`SandboxesLiveProvider`), their one source. */
export type ThreadsSnapshot = {
  threads: ThreadView[];
  more: boolean;
  archived_count: number;
  updates_connected: boolean;
  activity_current: boolean;
  watch: WatchHealth;
  sandbox_uid?: string;
};

export type { ThreadListing };
const DISABLED_CONNECTION: StreamConnection = { phase: "live", since: 0 };

export const THREAD_PAGE = 50;
const SEARCH_PAUSE_MS = 300;

interface ThreadListingState {
  listing: ThreadListing;
  setListing: (listing: ThreadListing) => void;
  /** The search box's text, which every thread list shows and edits; `listing.search` follows it
   * once typing pauses. */
  searchInput: string;
  setSearchInput: (text: string) => void;
}

const ThreadsLiveContext = createContext<Live<ThreadsSnapshot> | null>(null);
const ThreadListingContext = createContext<ThreadListingState | null>(null);

/** The Threads stream as one connection over its three watches. A failure that drops several (a network
 * change, a gateway roll) reaches each with the same reason, which the result names once. */
export function combinedConnection(
  history: StreamConnection,
  metadata: StreamConnection,
  inventory: StreamConnection
): StreamConnection {
  const sources = [history, metadata, inventory];
  const disconnected = sources.filter((source) => source.phase !== "live");
  if (disconnected.length === 0) {
    return { phase: "live", since: Math.max(...sources.map((source) => source.since)) };
  }
  const reconnecting = disconnected.filter((source) => source.phase === "reconnecting");
  const since = Math.min(...disconnected.map((source) => source.since));
  if (reconnecting.length === 0) return { phase: "connecting", since };
  const errors = reconnecting.flatMap((source) =>
    source.phase === "reconnecting" && source.lastError ? [source.lastError] : []
  );
  return {
    phase: "reconnecting",
    since,
    attempt: Math.max(...reconnecting.map((source) => (source.phase === "reconnecting" ? source.attempt : 0))),
    lastError: errors.length === 0 ? null : [...new Set(errors)].join("; "),
  };
}

function threadWatchHealth(connected: boolean, since: number | null): WatchHealth {
  return {
    healthy: connected,
    failing: connected
      ? {}
      : {
          thread_updates: {
            error: "PostgreSQL notification listener is disconnected",
            since: new Date(since ?? Date.now()).toISOString(),
          },
        },
  };
}

/** Dispatcher watch owns membership; App metadata and History watches refresh their independent joins. */
export function useThreadsLive(listing: ThreadListing | null, openThreadId: string | null): Live<ThreadsSnapshot> {
  const [state, setState] = useState<Pick<Live<ThreadsSnapshot>, "snapshot" | "health">>({
    snapshot: null,
    health: null,
  });
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const currentListing = useRef({ listing, openThreadId });
  currentListing.current = { listing, openThreadId };
  const refreshRef = useRef<() => void>(() => undefined);
  const sandboxUID = listing?.sandboxUID;
  const enabled = listing !== null;

  useEffect(() => {
    setState({ snapshot: null, health: null });
    setRefreshError(null);
    if (!enabled) return;
    let closed = false;
    let inventoryReady = false;
    let updatesConnected = false;
    let disconnectedSince: number | null = null;
    let metadata: Awaited<ReturnType<typeof listThreadMetadata>> | null = null;
    const sessions = new Map<string, Session>();
    const summaries = new Map<string, SessionSummary>();
    const lifetime = new AbortController();
    let metadataRunning = false;
    let metadataDirty = false;
    let retryAttempt = 0;
    let retryTimer: number | undefined;
    let inventoryConnection: StreamConnection = { phase: "connecting", since: Date.now() };
    let metadataConnection: StreamConnection = { phase: "connecting", since: Date.now() };
    let historyConnection: StreamConnection = { phase: "connecting", since: Date.now() };
    let watchKey: string | null = null;
    let stopSummaries: (() => void)[] = [];
    let summaryConnections: StreamConnection[] = [];
    let generation = 0;
    let snapshotPending = false;

    function reportConnections(): void {
      setConnection(combinedConnection(historyConnection, metadataConnection, inventoryConnection));
    }

    function publishActivity(): void {
      setState((previous) =>
        previous.snapshot === null
          ? previous
          : {
              ...previous,
              snapshot: {
                ...previous.snapshot,
                activity_current: inventoryReady && inventoryConnection.phase === "live",
                threads: previous.snapshot.threads.map((thread) => ({
                  ...thread,
                  session: sessions.get(thread.metadata.threadId),
                })),
              },
            }
      );
    }

    function publish(): void {
      if (closed || snapshotPending || !inventoryReady || metadata === null || currentListing.current.listing === null)
        return;
      const { listing: selected, openThreadId: open } = currentListing.current;
      const data = selectThreadWindow(selected!, open, [...sessions.values()], metadata, summaries);
      if (updatesConnected) disconnectedSince = null;
      else disconnectedSince ??= Date.now();
      const watch = threadWatchHealth(updatesConnected, disconnectedSince);
      setState({
        snapshot: {
          ...data,
          activity_current: inventoryConnection.phase === "live",
          sandbox_uid: selected!.sandboxUID,
          updates_connected: updatesConnected,
          watch,
        },
        health: watch,
      });
    }

    function refresh(): void {
      if (closed || !inventoryReady || metadata === null || currentListing.current.listing === null) return;
      const { listing: selected, openThreadId: open } = currentListing.current;
      const data = selectThreadWindow(selected!, open, [...sessions.values()], metadata, summaries);
      const ids = data.threads.map((thread) => thread.metadata.threadId).sort();
      const key = ids.join(",");
      if (key !== watchKey) {
        watchKey = key;
        generation++;
        const current = generation;
        stopSummaries.forEach((stop) => stop());
        stopSummaries = [];
        summaryConnections = [];
        const staged = new Map(summaries);
        const pending = new Set<number>();
        for (let start = 0; start < ids.length; start += 200) pending.add(start / 200);
        snapshotPending = pending.size > 0;
        if (ids.length === 0) historyConnection = DISABLED_CONNECTION;
        for (let start = 0; start < ids.length; start += 200) {
          const group = ids.slice(start, start + 200);
          const index = summaryConnections.length;
          summaryConnections.push({ phase: "connecting", since: Date.now() });
          const query = new URLSearchParams();
          group.forEach((id) => query.append("session_ids", id));
          stopSummaries.push(
            followWatch(`/upstream/history/v1/session-summaries:watchSelected?${query}`, {
              onOpen: () => {
                if (current === generation) {
                  pending.add(index);
                  snapshotPending = true;
                }
              },
              onFrame: (value) => {
                if (closed || current !== generation) return;
                const frame = fromJson(WatchSelectedSessionSummariesResponseSchema, value);
                switch (frame.observation.case) {
                  case "snapshot":
                    group.forEach((id) => staged.delete(id));
                    frame.observation.value.summaries.forEach((row) => staged.set(row.sessionId, row));
                    pending.delete(index);
                    break;
                  case "summary":
                    staged.set(frame.observation.value.sessionId, frame.observation.value);
                    break;
                  case "removedSessionId":
                    staged.delete(frame.observation.value);
                    break;
                  case "reconnectRequired":
                    return;
                  default:
                    throw new Error("History returned an empty selected summaries watch frame.");
                }
                if (pending.size === 0) {
                  summaries.clear();
                  staged.forEach((row, id) => summaries.set(id, row));
                  snapshotPending = false;
                  publish();
                }
              },
              onConnection: (next) => {
                if (current === generation) {
                  summaryConnections[index] = next;
                  const disconnected = summaryConnections.find((connection) => connection.phase !== "live");
                  historyConnection = disconnected ?? {
                    phase: "live",
                    since: Math.max(...summaryConnections.map((connection) => connection.since)),
                  };
                  reportConnections();
                }
              },
            })
          );
        }
      }
      reportConnections();
      publish();
    }

    function reloadMetadata(): void {
      if (closed) return;
      metadataDirty = true;
      window.clearTimeout(retryTimer);
      if (metadataRunning) return;
      metadataRunning = true;
      void (async () => {
        while (metadataDirty && !closed) {
          metadataDirty = false;
          try {
            const next = await listThreadMetadata(lifetime.signal);
            if (closed) return;
            metadata = next;
            retryAttempt = 0;
            setRefreshError(null);
            refresh();
          } catch (reason) {
            if (closed || lifetime.signal.aborted) return;
            metadataDirty = false;
            setRefreshError(displayableError(reason));
            retryTimer = window.setTimeout(reloadMetadata, backoff(retryAttempt++));
          }
        }
        metadataRunning = false;
      })();
    }
    refreshRef.current = refresh;
    const stopInventory = sessionDispatcherClient.watchSessions({
      onOpen: (snapshot) => {
        if (snapshot) sessions.clear();
        inventoryReady = false;
        publishActivity();
      },
      onFrame: (frame) => {
        switch (frame.frame.case) {
          case "session":
            sessions.set(frame.frame.value.sessionId, frame.frame.value);
            break;
          case "caughtUp":
            inventoryReady = true;
            break;
          case "reconnectRequired":
            inventoryReady = false;
            publishActivity();
            return;
        }
        if (inventoryReady) refresh();
      },
      onConnection: (next) => {
        inventoryConnection = next;
        reportConnections();
        publishActivity();
      },
    });
    const stopMetadata = followWatch("/upstream/app/v1/threads:watch", {
      onOpen: () => {},
      onFrame: (value) => {
        const frame = fromJson(WatchThreadMetadataResponseSchema, value);
        if (frame.changed === undefined) throw new Error("App returned an empty Thread metadata watch frame.");
        updatesConnected = frame.updatesConnected;
        reloadMetadata();
      },
      onConnection: (next) => {
        metadataConnection = next;
        reportConnections();
      },
    });
    return () => {
      closed = true;
      generation++;
      refreshRef.current = () => undefined;
      window.clearTimeout(retryTimer);
      lifetime.abort();
      stopInventory();
      stopMetadata();
      stopSummaries.forEach((stop) => stop());
    };
  }, [enabled, sandboxUID]);

  useEffect(() => {
    // Listing changes reuse the watched inventory; its CaughtUp frame gates the initial join.
    refreshRef.current();
  }, [listing, openThreadId]);

  const stalled = refreshError
    ? `thread list refresh failed: ${refreshError}`
    : state.snapshot && !state.snapshot.updates_connected
      ? "thread updates disconnected; showing the last snapshot"
      : null;
  const stream = useStreamStatus("Threads", enabled ? connection : DISABLED_CONNECTION, enabled ? stalled : null);
  return { ...state, stream };
}

/** Generated watches shared by navigation and the open composer. */
export function ThreadsLiveProvider({
  openThreadId,
  children,
}: {
  openThreadId: string | null;
  children: ReactNode;
}): JSX.Element {
  const [listing, setListing] = useState<ThreadListing>({ limit: THREAD_PAGE, search: null, includeArchived: false });
  const [searchInput, setSearchInput] = useState("");
  useEffect(() => {
    const wanted = searchInput.trim() || null;
    if (wanted === listing.search) return;
    const timer = window.setTimeout(
      () => setListing({ ...listing, search: wanted, limit: THREAD_PAGE }),
      SEARCH_PAUSE_MS
    );
    return () => window.clearTimeout(timer);
  }, [searchInput, listing]);
  const live = useThreadsLive(listing, openThreadId);
  return (
    <ThreadListingContext.Provider value={{ listing, setListing, searchInput, setSearchInput }}>
      <ThreadsLiveContext.Provider value={live}>{children}</ThreadsLiveContext.Provider>
    </ThreadListingContext.Provider>
  );
}

/** `ThreadsLiveProvider` for the Thread a `/threads/:threadId` route has open. */
export function RoutedThreadsLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const threadRoute = useMatch("/threads/:threadId");
  return <ThreadsLiveProvider openThreadId={threadRoute?.params.threadId ?? null}>{children}</ThreadsLiveProvider>;
}

export function useThreadListing(): ThreadListingState {
  const listing = useContext(ThreadListingContext);
  if (!listing) throw new Error("The thread listing requires ThreadsLiveProvider");
  return listing;
}

export function useRequiredThreadsLive(): Live<ThreadsSnapshot> {
  const live = useContext(ThreadsLiveContext);
  if (!live) throw new Error("Thread status requires ThreadsLiveProvider");
  return live;
}
