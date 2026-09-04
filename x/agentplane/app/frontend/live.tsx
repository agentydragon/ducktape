/**
 * The pushed views: one stream per page, and the honesty banner that goes with them.
 *
 * A page subscribes and renders what arrives; there is no fetch and no interval, because the
 * server holds a watch over the same objects and sends a whole snapshot whenever they change
 * (`live.py`). A snapshot replaces the page's state outright, so a reconnect needs no resume.
 *
 * The banner is the other half of that trade. A poll that stops shows an error on the next tick,
 * while a stream that goes quiet looks exactly like nothing happening, so `LiveStatus` says which
 * it is: the stream's own connection, and the server's verdict on whether its watch is still
 * cycling, are both on screen rather than assumed.
 *
 * A stream that has not opened yet is not a stream that failed, and `LiveStatus` says nothing
 * about it: a page begins in `connecting`, where every page load begins, and only an error or a
 * grace period with no frame at all makes it `disconnected`. Reporting the first millisecond of a
 * healthy load as a fault put an alarm on the screen of every load and moved the page under it.
 */
import { Alert } from "@mantine/core";
import { useEffect, useState } from "react";

import type { components } from "./api/schema";
import { api } from "./client";

export type WatchHealth = components["schemas"]["WatchHealth"];
export type SandboxesSnapshot = components["schemas"]["SandboxesSnapshot"];
export type SandboxSnapshot = components["schemas"]["SandboxSnapshot"];

/** Not yet opened, carrying frames, or failed -- the middle one is not a fault. */
export type Connection = "connecting" | "connected" | "disconnected";

export interface Live<T> {
  /** The last snapshot, or null until the first frame arrives. */
  snapshot: T | null;
  /** The watch's freshness, from the last frame of either kind. */
  health: WatchHealth | null;
  connection: Connection;
}

/**
 * How long a stream may stay silent before silence counts as failure. A server that accepts the
 * connection and then sends nothing never fires `error`, so without this the page would wait
 * forever with no snapshot and no explanation. Long enough that no ordinary load reaches it.
 */
const OPENING_GRACE_MS = 10_000;

export function liveSandboxesUrl(includeArchived: boolean): string {
  return `/live/sandboxes?include_archived=${includeArchived}`;
}

export function liveSandboxUrl(name: string): string {
  return `/live/sandboxes/${encodeURIComponent(name)}`;
}

export function useLive<T extends { watch: WatchHealth }>(url: string): Live<T> {
  const [state, setState] = useState<Live<T>>({ snapshot: null, health: null, connection: "connecting" });
  // A different object starts blank; the same one under a different filter does not. Only the path
  // says which this is -- the sandbox list's "show archived" is a query parameter, so resetting on
  // every url would empty the page and flash the disconnected banner each time it was toggled.
  const resource = new URL(url, window.location.origin).pathname;
  useEffect(() => setState({ snapshot: null, health: null, connection: "connecting" }), [resource]);
  useEffect(() => {
    let probed = false;
    const source = new EventSource(url);
    const silent = window.setTimeout(
      () =>
        setState((current) =>
          current.connection === "connecting" ? { ...current, connection: "disconnected" } : current
        ),
      OPENING_GRACE_MS
    );
    source.addEventListener("snapshot", (message: MessageEvent<string>) => {
      const snapshot = JSON.parse(message.data) as T;
      setState({ snapshot, health: snapshot.watch, connection: "connected" });
    });
    source.addEventListener("health", (message: MessageEvent<string>) => {
      const health = JSON.parse(message.data) as WatchHealth;
      setState((current) => ({ ...current, health, connection: "connected" }));
    });
    source.addEventListener("error", () => {
      // EventSource cannot see the status of a connection the server refused, so a stream that
      // fails before its first frame may be nothing worse than an expired session. One request
      // settles it: the API client sends the browser to log in on a 401.
      if (!probed) {
        probed = true;
        void api.GET("/models");
      }
      setState((current) => ({ ...current, connection: "disconnected" }));
    });
    return () => {
      window.clearTimeout(silent);
      source.close();
    };
  }, [url]);
  return state;
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

/** Nothing while the stream is live and the server's watch is moving; otherwise why it is not. */
export function LiveStatus<T>({ live }: { live: Live<T> }): JSX.Element | null {
  if (live.connection === "disconnected") {
    return (
      <Alert color="orange" p="xs">
        Not connected to the live stream; reconnecting. What is on screen may be out of date.
      </Alert>
    );
  }
  if (live.health && !live.health.fresh) {
    return (
      <Alert color="red" p="xs">
        The server&apos;s watch has stopped moving ({stalest(live.health)}), so this page is not being updated.
      </Alert>
    );
  }
  return null;
}
