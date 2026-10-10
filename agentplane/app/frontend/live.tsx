/**
 * The pushed views: one stream per page, and the honesty banner that goes with them.
 *
 * A page subscribes and renders what arrives; there is no fetch and no interval, because the
 * server holds a watch over the same objects and sends a whole snapshot whenever they change
 * (`live.py`). A snapshot replaces the page's state outright, so a reconnect needs no resume.
 *
 * The banner is the other half of that trade. A poll that stops shows an error on the next tick,
 * while a stream that goes quiet looks exactly like nothing happening, so neither is assumed: the
 * stream's own connection goes to the app's one connection indicator (`stream_status.tsx`), and
 * `LiveStatus` shows the server's verdict on whether its watch is still cycling.
 */
import { Alert } from "@mantine/core";
import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import { useMatch } from "react-router";

import type { components } from "./api/schema";
import { followStream, type StreamConnection } from "./live_stream";
import { type StreamStatus, useStreamStatus } from "./stream_status";

export type WatchHealth = components["schemas"]["WatchHealth"];
export type SandboxesSnapshot = components["schemas"]["SandboxesSnapshot"];
export type SandboxSnapshot = components["schemas"]["SandboxSnapshot"];
export type ThreadsSnapshot = components["schemas"]["ThreadsSnapshot"];

export interface Live<T> {
  /** The last snapshot, or null until the first frame arrives. */
  snapshot: T | null;
  /** The watch's freshness, from the last frame of either kind. */
  health: WatchHealth | null;
  stream: StreamStatus;
}

export function liveSandboxesUrl(): string {
  return "/live/sandboxes";
}

/** What the sidebar asks of the thread list: a window of its newest threads, which grows a page at
 * a time, and the filters it is read under. */
export interface ThreadListing {
  limit: number;
  search: string | null;
  includeArchived: boolean;
}

export const THREAD_PAGE = 50;

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

/** The stream at `url`, which the connection indicator calls `name`. */
export function useLive<T extends { watch: WatchHealth }>(url: string, name: string): Live<T> {
  const [state, setState] = useState<Pick<Live<T>, "snapshot" | "health">>({ snapshot: null, health: null });
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  // A different object starts blank; the same one under a different filter does not. Only the path
  // says which this is, so a query-string-only change resets nothing.
  const resource = new URL(url, window.location.origin).pathname;
  useEffect(() => setState({ snapshot: null, health: null }), [resource]);
  useEffect(
    () =>
      followStream(url, {
        events: {
          snapshot: (message) => {
            const snapshot = JSON.parse(message.data) as T;
            setState({ snapshot, health: snapshot.watch });
          },
          health: (message) => {
            const health = JSON.parse(message.data) as WatchHealth;
            setState((current) => ({ ...current, health }));
          },
        },
        onConnection: setConnection,
      }),
    [url]
  );
  const stream = useStreamStatus(name, connection);
  return { ...state, stream };
}

const ThreadsLiveContext = createContext<Live<ThreadsSnapshot> | null>(null);
const ThreadListingContext = createContext<{
  listing: ThreadListing;
  setListing: (listing: ThreadListing) => void;
} | null>(null);
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
  const live = useLive<ThreadsSnapshot>(liveThreadsUrl(listing, openThreadId), "Threads");
  return (
    <ThreadListingContext.Provider value={{ listing, setListing }}>
      <ThreadsLiveContext.Provider value={live}>{children}</ThreadsLiveContext.Provider>
    </ThreadListingContext.Provider>
  );
}

/** `ThreadsLiveProvider` for the Thread a `/threads/:threadId` route has open. */
export function RoutedThreadsLiveProvider({ children }: { children: ReactNode }): JSX.Element {
  const threadRoute = useMatch("/threads/:threadId");
  return <ThreadsLiveProvider openThreadId={threadRoute?.params.threadId ?? null}>{children}</ThreadsLiveProvider>;
}

export function useThreadListing(): { listing: ThreadListing; setListing: (listing: ThreadListing) => void } {
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
  const live = useLive<SandboxesSnapshot>(liveSandboxesUrl(), "Sandboxes");
  return <SandboxesLiveContext.Provider value={live}>{children}</SandboxesLiveContext.Provider>;
}

export function useRequiredSandboxesLive(): Live<SandboxesSnapshot> {
  const live = useContext(SandboxesLiveContext);
  if (!live) throw new Error("Sandbox status requires SandboxesLiveProvider");
  return live;
}

const AGE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** An age a reader takes in at a glance: "40 minutes ago", not the 2417 seconds behind it. */
function humanAge(seconds: number): string {
  const [amount, unit]: [number, Intl.RelativeTimeFormatUnit] =
    seconds < 90 ? [seconds, "second"] : seconds < 5400 ? [seconds / 60, "minute"] : [seconds / 3600, "hour"];
  return AGE.format(-Math.round(amount), unit);
}

/** The oldest watched kind and how far behind it is, as the server last reported. */
function stalest(health: WatchHealth): string {
  const [kind, age] = Object.entries(health.refreshed_seconds_ago).reduce(
    (oldest, entry) => (entry[1] > oldest[1] ? entry : oldest),
    ["nothing", 0]
  );
  return `${kind} last updated ${humanAge(age)}`;
}

/** Nothing while the server's watch is moving; otherwise how far behind it is. */
export function LiveStatus<T>({ live }: { live: Live<T> }): JSX.Element | null {
  if (!live.health || live.health.fresh) return null;
  return (
    <Alert color="red" p="xs">
      The server&apos;s watch has stopped moving ({stalest(live.health)}), so this page is not being updated.
    </Alert>
  );
}
