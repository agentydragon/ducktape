/**
 * Every live stream's connection in one place, and what the app shows of it.
 *
 * A stream that drops and comes back within seconds -- a network change, a server roll -- leaves its
 * page no worse off, and saying so on screen each time only teaches the reader to ignore it. So a
 * stream counts as degraded only once it has been off for `DEGRADED_AFTER_MS` without a break, and
 * the topbar shows a spinner for it; at `STALE_AFTER_MS` the spinner turns amber. A stream that is
 * connected but whose server-side source reports its watch unhealthy is stale at once, with the reason in
 * `stalled`. The spinner is the only place any of this shows; the details are in its tooltip.
 */
import { Box, Loader, Tooltip } from "@mantine/core";
import { type JSX, useEffect, useState, useSyncExternalStore } from "react";

import { isoTime, timeAgo } from "./format_time";
import type { StreamConnection } from "./stream_reader";

export const DEGRADED_AFTER_MS = 5_000;
export const STALE_AFTER_MS = 60_000;

/** `current`: live, or off for less than `DEGRADED_AFTER_MS`. */
export type Standing = "current" | "degraded" | "stale";

export interface StreamStatus {
  /** What the indicator calls it: the page or panel the stream feeds. */
  name: string;
  connection: StreamConnection;
  /** Why the server says the stream's data is not moving though the connection is up, if it does. */
  stalled: string | null;
  standing: Standing;
}

function standingAt(connection: StreamConnection, stalled: string | null, now: number): Standing {
  if (stalled !== null) return "stale";
  if (connection.phase === "live") return "current";
  const off = now - connection.since;
  return off >= STALE_AFTER_MS ? "stale" : off >= DEGRADED_AFTER_MS ? "degraded" : "current";
}

export class StreamRegistry {
  /** The clock the thresholds are measured on. The visual harness sets it ahead of the frozen `Date`
   * its captures run under, since a scene cannot wait out real seconds. */
  now: () => number = () => Date.now();
  readonly #streams = new Map<symbol, { name: string; connection: StreamConnection; stalled: string | null }>();
  readonly #listeners = new Set<() => void>();
  #statuses: ReadonlyMap<symbol, StreamStatus> = new Map();
  #crossing: number | undefined;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  getStatuses = (): ReadonlyMap<symbol, StreamStatus> => this.#statuses;

  report(key: symbol, name: string, connection: StreamConnection, stalled: string | null): void {
    this.#streams.set(key, { name, connection, stalled });
    this.#derive();
  }

  remove(key: symbol): void {
    if (this.#streams.delete(key)) this.#derive();
  }

  /** Every stream's standing now, and a wake-up for the next threshold one of them crosses. A status
   * that has not changed stays the same object, so a reader of one stream renders only for it. */
  #derive(): void {
    window.clearTimeout(this.#crossing);
    const now = this.now();
    let next = Infinity;
    const statuses = new Map<symbol, StreamStatus>();
    for (const [key, { name, connection, stalled }] of this.#streams) {
      const standing = standingAt(connection, stalled, now);
      const previous = this.#statuses.get(key);
      statuses.set(
        key,
        previous?.name === name &&
          previous.connection === connection &&
          previous.stalled === stalled &&
          previous.standing === standing
          ? previous
          : { name, connection, stalled, standing }
      );
      if (connection.phase === "live") continue;
      for (const threshold of [DEGRADED_AFTER_MS, STALE_AFTER_MS]) {
        const until = connection.since + threshold - now;
        if (until > 0) next = Math.min(next, until);
      }
    }
    this.#statuses = statuses;
    if (next !== Infinity) this.#crossing = window.setTimeout(() => this.#derive(), next);
    for (const listener of this.#listeners) listener();
  }
}

export const streamRegistry: StreamRegistry = new StreamRegistry();

/** Reports `connection` as the stream `name` for as long as the caller is mounted, or nothing while
 * it is null, and reads back what the registry has made of it. */
function useReported(
  name: string,
  connection: StreamConnection | null,
  stalled: string | null
): StreamStatus | undefined {
  const [key] = useState(() => Symbol(name));
  useEffect(() => {
    if (connection === null) streamRegistry.remove(key);
    else streamRegistry.report(key, name, connection, stalled);
  }, [key, name, connection, stalled]);
  useEffect(() => () => streamRegistry.remove(key), [key]);
  return useSyncExternalStore(streamRegistry.subscribe, () => streamRegistry.getStatuses().get(key));
}

/** What `reported` says of `connection`, or until the report lands, the connection as it stands now. */
function statusOf(
  reported: StreamStatus | undefined,
  name: string,
  connection: StreamConnection,
  stalled: string | null
): StreamStatus {
  return reported?.connection === connection && reported.stalled === stalled
    ? reported
    : { name, connection, stalled, standing: standingAt(connection, stalled, streamRegistry.now()) };
}

/** Reports `connection` as the stream `name` for as long as the caller is mounted, and reads back
 * how the stream stands. */
export function useStreamStatus(
  name: string,
  connection: StreamConnection,
  stalled: string | null = null
): StreamStatus {
  return statusOf(useReported(name, connection, stalled), name, connection, stalled);
}

/** As `useStreamStatus`, for a stream that may not exist: nothing while `connection` is null. */
export function useOptionalStreamStatus(name: string, connection: StreamConnection | null): StreamStatus | null {
  const reported = useReported(name, connection, null);
  return connection === null ? null : statusOf(reported, name, connection, null);
}

function describe({ name, connection, stalled }: StreamStatus): string {
  const parts = [`${name}: ${connection.phase} since ${timeAgo(connection.since)} (${isoTime(connection.since)})`];
  if (connection.phase === "reconnecting") {
    parts.push(`attempt ${connection.attempt}`);
    if (connection.lastError !== null) parts.push(connection.lastError);
  }
  if (stalled !== null) parts.push(`${stalled}; the server keeps retrying`);
  return parts.join(" · ");
}

/** Nothing while every stream is current; otherwise a spinner, amber once one is stale, whose
 * tooltip names each stream that is not, since when and why. Every stream retries by itself. */
export function ConnectionIndicator(): JSX.Element | null {
  const statuses = useSyncExternalStore(streamRegistry.subscribe, streamRegistry.getStatuses);
  const affected = [...statuses.values()].filter((status) => status.standing !== "current");
  if (affected.length === 0) return null;
  const standing = affected.some((status) => status.standing === "stale") ? "stale" : "degraded";
  const lines = affected.map(describe);
  return (
    <Tooltip
      label={lines.map((line) => (
        <div key={line}>{line}</div>
      ))}
      multiline
      withArrow
      events={{ hover: true, focus: true, touch: true }}
    >
      <Box
        component="span"
        role="img"
        tabIndex={0}
        aria-label={lines.join("; ")}
        data-connection={standing}
        style={{ display: "inline-flex" }}
      >
        <Loader size="xs" color={standing === "stale" ? "yellow" : "gray"} />
      </Box>
    </Tooltip>
  );
}
