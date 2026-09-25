/**
 * Every live stream's connection in one place, and what the app shows of it.
 *
 * A stream that drops and comes back within seconds -- a network change, a server roll -- leaves its
 * page no worse off, and saying so on screen each time only teaches the reader to ignore it. So a
 * stream counts as degraded only once it has been off for `DEGRADED_AFTER_MS` without a break, and
 * the sidebar's footer shows a spinner for it; at `STALE_AFTER_MS` the spinner turns amber, and a
 * page whose own stream it is says that what it shows may be out of date.
 */
import { Alert, Box, Loader, Tooltip } from "@mantine/core";
import { type JSX, useEffect, useState, useSyncExternalStore } from "react";

import type { StreamConnection } from "./live_stream";

export const DEGRADED_AFTER_MS = 5_000;
export const STALE_AFTER_MS = 60_000;

/** `current`: live, or off for less than `DEGRADED_AFTER_MS`. */
export type Standing = "current" | "degraded" | "stale";

export interface StreamStatus {
  /** What the indicator calls it: the page or panel the stream feeds. */
  name: string;
  connection: StreamConnection;
  standing: Standing;
}

function standingAt(connection: StreamConnection, now: number): Standing {
  if (connection.phase === "live") return "current";
  const off = now - connection.since;
  return off >= STALE_AFTER_MS ? "stale" : off >= DEGRADED_AFTER_MS ? "degraded" : "current";
}

export class StreamRegistry {
  /** The clock the thresholds are measured on. The visual harness sets it ahead of the frozen `Date`
   * its captures run under, since a scene cannot wait out real seconds. */
  now: () => number = () => Date.now();
  readonly #streams = new Map<symbol, { name: string; connection: StreamConnection }>();
  readonly #listeners = new Set<() => void>();
  #statuses: ReadonlyMap<symbol, StreamStatus> = new Map();
  #crossing: number | undefined;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  getStatuses = (): ReadonlyMap<symbol, StreamStatus> => this.#statuses;

  report(key: symbol, name: string, connection: StreamConnection): void {
    this.#streams.set(key, { name, connection });
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
    for (const [key, { name, connection }] of this.#streams) {
      const standing = standingAt(connection, now);
      const previous = this.#statuses.get(key);
      statuses.set(
        key,
        previous?.name === name && previous.connection === connection && previous.standing === standing
          ? previous
          : { name, connection, standing }
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
 * it is null, and reads back how the stream stands. */
export function useStreamStatus(name: string, connection: StreamConnection): StreamStatus;
export function useStreamStatus(name: string, connection: StreamConnection | null): StreamStatus | null;
export function useStreamStatus(name: string, connection: StreamConnection | null): StreamStatus | null {
  const [key] = useState(() => Symbol(name));
  useEffect(() => {
    if (connection === null) streamRegistry.remove(key);
    else streamRegistry.report(key, name, connection);
  }, [key, name, connection]);
  useEffect(() => () => streamRegistry.remove(key), [key]);
  const reported = useSyncExternalStore(streamRegistry.subscribe, () => streamRegistry.getStatuses().get(key));
  if (connection === null) return null;
  // Until the report above lands, the connection as it stands now.
  return reported?.connection === connection
    ? reported
    : { name, connection, standing: standingAt(connection, streamRegistry.now()) };
}

const CLOCK = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function describe({ name, connection }: StreamStatus): string {
  const parts = [`${name}: ${connection.phase} since ${CLOCK.format(connection.since)}`];
  if (connection.phase === "reconnecting") {
    parts.push(`attempt ${connection.attempt}`);
    if (connection.lastError !== null) parts.push(connection.lastError);
  }
  return parts.join(" · ");
}

/** Nothing while every stream is current; otherwise a spinner, amber once one is stale, whose
 * tooltip names each stream that is not and since when. */
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

/** A page's own streams: nothing until one is stale, then when the page last heard from it. */
export function StaleNotice({ streams }: { streams: readonly (StreamStatus | null)[] }): JSX.Element | null {
  const since = streams.flatMap((stream) => (stream?.standing === "stale" ? [stream.connection.since] : []));
  if (since.length === 0) return null;
  return (
    <Alert color="yellow" p="xs">
      What&apos;s on screen may be out of date; last update {CLOCK.format(Math.min(...since))}
    </Alert>
  );
}
