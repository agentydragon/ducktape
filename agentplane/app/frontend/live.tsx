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
import { type JSX, useEffect, useState } from "react";

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

export function liveThreadsUrl(): string {
  return "/live/threads";
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
