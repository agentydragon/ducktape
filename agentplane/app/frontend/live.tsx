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
import { useEffect, useState } from "react";

import type { WatchHealth } from "./api/schema";
import { ageAgo } from "./format_time";
import { followStream, type StreamConnection } from "./live_stream";
import { type StreamStatus, useStreamStatus } from "./stream_status";

export interface Live<T> {
  /** The last snapshot, or null until the first frame arrives. */
  snapshot: T | null;
  /** The watch's freshness, from the last frame of either kind. */
  health: WatchHealth | null;
  stream: StreamStatus;
}
/** The stream at `url`, which the connection indicator calls `name`. */
export function useLive<T extends { watch: WatchHealth }>(
  url: string,
  name: string,
  /** Why the last snapshot's own content says its data is not moving, where it can. */
  stalledBy: (snapshot: T) => string | null = () => null
): Live<T> {
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
  const stalled =
    state.health && !state.health.fresh
      ? `watch stopped moving (${stalest(state.health)})`
      : state.snapshot && stalledBy(state.snapshot);
  const stream = useStreamStatus(name, connection, stalled);
  return { ...state, stream };
}
/** The oldest watched kind and how far behind it is, as the server last reported. */
function stalest(health: WatchHealth): string {
  const [kind, age] = Object.entries(health.refreshed_seconds_ago).reduce(
    (oldest, entry) => (entry[1] > oldest[1] ? entry : oldest),
    ["nothing", 0]
  );
  return `${kind} last updated ${ageAgo(age)}`;
}
