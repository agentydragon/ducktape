import { createContext, type JSX, type ReactNode, useContext, useEffect, useState } from "react";
import { useMatch } from "react-router";

import { useLive, type Live, type WatchHealth } from "../live";

import type { ThreadView } from "../schema";

/** The Thread window. Sandboxes come from `/live/sandboxes` (`SandboxesLiveProvider`), their one source. */
export type ThreadsSnapshot = {
  threads: ThreadView[];
  more: boolean;
  archived_count: number;
  updates_connected: boolean;
  watch: WatchHealth;
};

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
  const live = useLive<ThreadsSnapshot>(liveThreadsUrl(listing, openThreadId), "Threads", (snapshot) =>
    snapshot.updates_connected ? null : "thread updates disconnected; showing the last snapshot"
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
