/**
 * The pushed views: one stream per page, and the honesty that goes with them.
 *
 * A page subscribes and renders what arrives; there is no fetch and no interval, because the
 * server holds a watch over the same objects and sends a whole snapshot whenever they change
 * (`live.py`). A snapshot replaces the page's state outright, so a reconnect needs no resume.
 *
 * Honesty is the other half of that trade. A poll that stops shows an error on the next tick,
 * while a stream that goes quiet looks exactly like nothing happening, so neither is assumed: the
 * stream's connection and the server's verdict on whether its watch is still cycling both go to the
 * app's one connection indicator (`stream_status.tsx`).
 */
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import { useMatch } from "react-router";
import type { z } from "zod";

import type { components } from "./api/schema";
import { zSandboxSnapshot, zSandboxesSnapshot, zThreadsSnapshot, zWatchHealth } from "./api/live_schema.zod";
import { ageAgo } from "./format_time";
import { followStream, parseJsonFrame, type StreamConnection } from "./live_stream";
import { type StreamStatus, useStreamStatus } from "./stream_status";

export type WatchHealth = components["schemas"]["WatchHealth"];
export type SandboxesSnapshot = components["schemas"]["SandboxesSnapshot"];
export type SandboxSnapshot = components["schemas"]["SandboxSnapshot"];
export type ThreadsSnapshot = components["schemas"]["ThreadsSnapshot"];
export type DecisionsSnapshot = components["schemas"]["DecisionsSnapshot"];

export interface Live<T> {
  /** The last snapshot, or null until the first frame arrives. */
  snapshot: T | null;
  /** The watch's freshness, from the last frame of either kind. */
  health: WatchHealth | null;
  stream: StreamStatus;
}

type SnapshotSchemas<T> = { snapshot: z.ZodType<T>; health: z.ZodType<WatchHealth> };

/** Runtime validators generated from the Pydantic models that publish these streams. */
export const liveSchemas = {
  sandbox: { snapshot: zSandboxSnapshot, health: zWatchHealth },
  sandboxes: { snapshot: zSandboxesSnapshot, health: zWatchHealth },
  threads: { snapshot: zThreadsSnapshot, health: zWatchHealth },
} satisfies {
  sandbox: SnapshotSchemas<SandboxSnapshot>;
  sandboxes: SnapshotSchemas<SandboxesSnapshot>;
  threads: SnapshotSchemas<ThreadsSnapshot>;
};

export function liveSandboxesUrl(): string {
  return "/live/sandboxes";
}

/** What the thread lists ask of `/live/threads`: a window of the newest threads, which grows a page
 * at a time, and the filters it is read under. */
export interface ThreadListing {
  limit: number;
  search: string | null;
  includeArchived: boolean;
}

export const THREAD_PAGE = 50;
const SEARCH_PAUSE_MS = 300;

export function liveThreadsUrl(listing: ThreadListing, openThreadId: string | null): string {
  const query = new URLSearchParams({
    limit: String(listing.limit),
    include_archived: String(listing.includeArchived),
  });
  if (listing.search !== null) query.set("search", listing.search);
  if (openThreadId !== null) query.set("open_thread", openThreadId);
  return `/live/threads?${query}`;
}

export function liveSandboxUrl(name: string, includeArchived: boolean): string {
  return `/live/sandboxes/${encodeURIComponent(name)}?include_archived=${includeArchived}`;
}

export function liveEgressDecisionsUrl(name: string): string {
  return `/live/sandboxes/${encodeURIComponent(name)}/egress/decisions`;
}

/** The stream at `url`, which the connection indicator calls `name`. */
export function useLive<T extends { watch: WatchHealth }>(
  url: string,
  name: string,
  schemas: SnapshotSchemas<T>,
  /** Why the last snapshot's own content says its data is not moving, where it can. */
  stalledBy: (snapshot: T) => string | null = () => null
): Live<T> {
  const [state, setState] = useState({
    snapshot: null as T | null,
    health: null as WatchHealth | null,
    snapshotError: null as string | null,
    healthError: null as string | null,
  });
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  // A different object starts blank; the same one under a different filter does not. Only the path
  // says which this is, so a query-string-only change resets nothing.
  const resource = new URL(url, window.location.origin).pathname;
  useEffect(() => setState({ snapshot: null, health: null, snapshotError: null, healthError: null }), [resource]);
  useEffect(
    () =>
      followStream(url, {
        events: {
          snapshot: (message) => {
            const parsed = parseJsonFrame(schemas.snapshot, message.data, "Snapshot");
            if (!parsed.success) {
              setState((current) => ({ ...current, snapshotError: parsed.error }));
              return parsed.error;
            }
            setState({ snapshot: parsed.data, health: parsed.data.watch, snapshotError: null, healthError: null });
          },
          health: (message) => {
            const parsed = parseJsonFrame(schemas.health, message.data, "Health");
            if (!parsed.success) {
              setState((current) => ({ ...current, healthError: parsed.error }));
              return parsed.error;
            }
            setState((current) => ({ ...current, health: parsed.data, healthError: null }));
          },
        },
        onConnection: setConnection,
      }),
    [schemas.snapshot, schemas.health, url]
  );
  const frameError = state.snapshotError ?? state.healthError;
  const stalled =
    frameError ??
    (state.health && !state.health.fresh
      ? `watch stopped moving (${stalest(state.health)})`
      : state.snapshot && stalledBy(state.snapshot));
  const stream = useStreamStatus(name, connection, stalled);
  return { snapshot: state.snapshot, health: state.health, stream };
}

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
const SandboxesLiveContext = createContext<Live<SandboxesSnapshot> | null>(null);

/** One /live/threads connection for the shell, shared by navigation and the open composer. It always
 * carries the open Thread, so that Thread's page stays current however far down the list it is. */
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
    // Each listing is its own stream, so typing waits for a pause before asking for one.
    const wanted = searchInput.trim() || null;
    if (wanted === listing.search) return;
    const timer = window.setTimeout(
      () => setListing({ ...listing, search: wanted, limit: THREAD_PAGE }),
      SEARCH_PAUSE_MS
    );
    return () => window.clearTimeout(timer);
  }, [searchInput, listing]);
  const live = useLive<ThreadsSnapshot>(
    liveThreadsUrl(listing, openThreadId),
    "Threads",
    liveSchemas.threads,
    (snapshot) => (snapshot.updates_connected ? null : "thread updates disconnected; showing the last snapshot")
  );
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

/** One /live/sandboxes connection for thread routes that need current runner availability. */
export function SandboxesLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const live = useLive<SandboxesSnapshot>(liveSandboxesUrl(), "Sandboxes", liveSchemas.sandboxes);
  return <SandboxesLiveContext.Provider value={live}>{children}</SandboxesLiveContext.Provider>;
}

export function useRequiredSandboxesLive(): Live<SandboxesSnapshot> {
  const live = useContext(SandboxesLiveContext);
  if (!live) throw new Error("Sandbox status requires SandboxesLiveProvider");
  return live;
}

/** The oldest watched kind and how far behind it is, as the server last reported. */
function stalest(health: WatchHealth): string {
  const [kind, age] = Object.entries(health.refreshed_seconds_ago).reduce(
    (oldest, entry) => (entry[1] > oldest[1] ? entry : oldest),
    ["nothing", 0]
  );
  return `${kind} last updated ${ageAgo(age)}`;
}
