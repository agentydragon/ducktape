/**
 * The pushed views: one stream per page, and the honesty that goes with them.
 *
 * A page subscribes and renders what arrives; there is no fetch and no interval, because the
 * server holds a watch over the same objects and sends a whole snapshot whenever they change
 * (`live.py`). A snapshot replaces the page's state outright, so a reconnect needs no resume.
 *
 * Honesty is the other half of that trade. A poll that stops shows an error on the next tick,
 * while a stream that goes quiet looks exactly like nothing happening, so neither is assumed: the
 * stream's connection and the server's verdict on whether its watch is healthy both go to the app's
 * one connection indicator (`stream_status.tsx`).
 */
import { useEffect, useState } from "react";

import { ageAgo } from "./format_time";
import { followStream } from "./live_stream";
import { type StreamConnection } from "./stream_reader";
import { type StreamStatus, useStreamStatus } from "./stream_status";

// The raw live streams use app/go's watchHealthView JSON shape.
export type WatchHealth = {
  /** Whether the server's informers have synced and no watched kind is failing to list or watch. */
  healthy: boolean;
  /** Each failing kind, with its error and since when (RFC 3339). */
  failing: Record<string, { error: string; since: string }>;
};
export interface Live<T> {
  /** The last snapshot, or null until the first frame arrives. */
  snapshot: T | null;
  /** The watch's health, from the last frame of either kind. */
  health: WatchHealth | null;
  stream: StreamStatus;
}
const DISABLED_CONNECTION: StreamConnection = { phase: "live", since: 0 };
/** The stream at `url`, which the connection indicator calls `name`. */
export function useLive<T extends { watch: WatchHealth }>(
  url: string | null,
  name: string,
  /** Why the last snapshot's own content says its data is not moving, where it can. */
  stalledBy: (snapshot: T) => string | null = () => null
): Live<T> {
  const [state, setState] = useState<Pick<Live<T>, "snapshot" | "health">>({ snapshot: null, health: null });
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  // A different object, including a same-name Sandbox with a new UID, starts blank.
  const resource = url === null ? null : new URL(url, window.location.origin);
  const identity =
    resource === null ? null : `${resource.pathname}?sandbox_uid=${resource.searchParams.get("sandbox_uid") ?? ""}`;
  useEffect(() => setState({ snapshot: null, health: null }), [identity]);
  useEffect(() => {
    if (url === null) return;
    let active = true;
    const stop = followStream(url, {
      events: {
        snapshot: (message) => {
          if (!active) return;
          const snapshot = JSON.parse(message.data) as T;
          setState({ snapshot, health: snapshot.watch });
        },
        health: (message) => {
          if (!active) return;
          const health = JSON.parse(message.data) as WatchHealth;
          setState((current) => ({ ...current, health }));
        },
      },
      onConnection: (next) => {
        if (active) setConnection(next);
      },
    });
    return () => {
      active = false;
      stop();
    };
  }, [url]);
  const stalled =
    state.health && !state.health.healthy ? unhealthy(state.health) : state.snapshot && stalledBy(state.snapshot);
  const stream = useStreamStatus(name, url === null ? DISABLED_CONNECTION : connection, url === null ? null : stalled);
  return { ...state, stream };
}
/** Why the server's watch is not healthy, as it last reported: each failing kind, or not yet synced. */
function unhealthy(health: WatchHealth): string {
  const failing = Object.entries(health.failing);
  if (failing.length === 0) {
    return "watch not yet synced";
  }
  const reasons = failing.map(
    ([kind, failure]) =>
      `${kind} failing since ${ageAgo((Date.now() - Date.parse(failure.since)) / 1000)}: ${failure.error}`
  );
  return `watch failing (${reasons.join("; ")})`;
}
