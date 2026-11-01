import { fromJson, type JsonValue } from "@bufbuild/protobuf";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { useMatch } from "react-router";

import { displayableError } from "../client";
import { type Live, type WatchHealth } from "../live";
import { backoff, type StreamConnection } from "../stream_reader";
import { useStreamStatus } from "../stream_status";
import { followWatch } from "../watch_stream";
import { WatchThreadMetadataResponseSchema } from "../../thread_metadata/thread_metadata_pb";
import { WatchSessionSummariesResponseSchema } from "../../../history_service/history_service_pb";

import type { ThreadView } from "../schema";
import { listThreadWindow, type ThreadListing } from "./client";

/** The Thread window. Sandboxes come from `/live/sandboxes` (`SandboxesLiveProvider`), their one source. */
export type ThreadsSnapshot = {
  threads: ThreadView[];
  more: boolean;
  archived_count: number;
  updates_connected: boolean;
  watch: WatchHealth;
};

export type { ThreadListing };

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

function protoJson(value: unknown): JsonValue {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (Array.isArray(value)) return value.map(protoJson);
  if (typeof value === "object") {
    const result: { [key: string]: JsonValue } = {};
    for (const [key, child] of Object.entries(value)) result[key] = protoJson(child);
    return result;
  }
  throw new Error("Thread watch returned a non-JSON value.");
}

function combinedConnection(history: StreamConnection, metadata: StreamConnection): StreamConnection {
  const sources = [history, metadata];
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
    lastError: errors.length === 0 ? null : errors.join("; "),
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

/** Follows History and app-owned metadata watches, joining their generated reads in the browser. */
function useThreadsLive(listing: ThreadListing, openThreadId: string | null): Live<ThreadsSnapshot> {
  const [state, setState] = useState<Pick<Live<ThreadsSnapshot>, "snapshot" | "health">>({
    snapshot: null,
    health: null,
  });
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const currentListing = useRef({ listing, openThreadId });
  currentListing.current = { listing, openThreadId };
  const refreshRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    let closed = false;
    let running = false;
    let dirty = false;
    let retryAttempt = 0;
    let retryTimer: number | undefined;
    let updatesConnected = false;
    let disconnectedSince: number | null = null;
    let historyReady = false;
    let metadataReady = false;
    const lifetime = new AbortController();
    let historyConnection: StreamConnection = { phase: "connecting", since: Date.now() };
    let metadataConnection: StreamConnection = { phase: "connecting", since: Date.now() };

    function reportConnections(): void {
      setConnection(combinedConnection(historyConnection, metadataConnection));
    }

    function refresh(): void {
      if (closed) return;
      window.clearTimeout(retryTimer);
      retryTimer = undefined;
      dirty = true;
      if (running) return;
      running = true;
      void (async () => {
        while (dirty && !closed) {
          dirty = false;
          try {
            const current = currentListing.current;
            const windowData = await listThreadWindow(current.listing, current.openThreadId, lifetime.signal);
            if (closed) return;
            if (updatesConnected) disconnectedSince = null;
            else disconnectedSince ??= Date.now();
            const watch = threadWatchHealth(updatesConnected, disconnectedSince);
            const snapshot = { ...windowData, updates_connected: updatesConnected, watch };
            setState({ snapshot, health: watch });
            setRefreshError(null);
            retryAttempt = 0;
          } catch (reason) {
            if (closed || lifetime.signal.aborted) return;
            dirty = false;
            setRefreshError(displayableError(reason));
            retryTimer = window.setTimeout(() => {
              retryTimer = undefined;
              refresh();
            }, backoff(retryAttempt++));
          }
        }
        running = false;
      })();
    }

    refreshRef.current = refresh;
    const stopMetadata = followWatch("/upstream/app/v1/threads:watch", {
      onOpen: () => {
        metadataReady = false;
      },
      onFrame: (value) => {
        const frame = fromJson(WatchThreadMetadataResponseSchema, protoJson(value));
        if (frame.changed === undefined) throw new Error("App returned an empty Thread metadata watch frame.");
        updatesConnected = frame.updatesConnected;
        metadataReady = true;
        if (historyReady) refresh();
      },
      onConnection: (next) => {
        metadataConnection = next;
        reportConnections();
      },
    });
    const stopHistory = followWatch("/upstream/history/v1/session-summaries:watch", {
      onOpen: () => {
        historyReady = false;
      },
      onFrame: (value) => {
        const frame = fromJson(WatchSessionSummariesResponseSchema, protoJson(value));
        switch (frame.observation.case) {
          case "changed":
          case "reconnectRequired":
          case "sessionOpened":
            historyReady = true;
            if (metadataReady) refresh();
            break;
          case undefined:
            throw new Error("History Service returned an empty Session summary watch frame.");
        }
      },
      onConnection: (next) => {
        historyConnection = next;
        reportConnections();
      },
    });

    return () => {
      closed = true;
      refreshRef.current = () => undefined;
      window.clearTimeout(retryTimer);
      lifetime.abort();
      stopHistory();
      stopMetadata();
    };
  }, []);

  useEffect(() => {
    // Render the initial read even if a watch is slow to open. Once both watches have sent their
    // opening frames, the watch callbacks refresh again to cover changes during this read.
    refreshRef.current();
  }, [listing, openThreadId]);

  const stalled = refreshError
    ? `thread list refresh failed: ${refreshError}`
    : state.snapshot && !state.snapshot.updates_connected
      ? "thread updates disconnected; showing the last snapshot"
      : null;
  const stream = useStreamStatus("Threads", connection, stalled);
  return { ...state, stream };
}

/** One pair of generated watches shared by navigation and the open composer. */
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
