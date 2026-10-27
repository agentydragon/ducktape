/**
 * An `EventSource` kept open for as long as a page follows it.
 *
 * The browser reconnects a stream the network dropped by itself: the source goes back to
 * `CONNECTING` and retries. A response that is not a stream -- any non-2xx, an expired login's 401
 * among them -- closes it for good instead, and nothing retries. So a closed source asks the API
 * whether the login still holds, which sends the browser to log in if not (`client.ts`), and
 * otherwise opens a new source after a backoff; sooner once the browser is back online or the page
 * is back in view.
 */
import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import type { z } from "zod";

import { api } from "./client";

/** `since` is when the stream entered the phase; `reconnecting` covers every failure since it was
 * last live, a failed first connection included, and `attempt` counts them. `lastError` is why the
 * last one failed, where the stream can tell: an `EventSource` cannot. */
export type StreamConnection =
  | { phase: "connecting"; since: number }
  | { phase: "live"; since: number }
  | { phase: "reconnecting"; since: number; attempt: number; lastError: string | null }
  | { phase: "invalid"; since: number; lastError: string };

const FIRST_RETRY_MS = 1_000;
const LAST_RETRY_MS = 30_000;

/** Between half and all of a delay that doubles with each retry, so the pages that lost one server
 * do not all come back to it at once. */
export function backoff(retries: number): number {
  const ceiling = Math.min(LAST_RETRY_MS, FIRST_RETRY_MS * 2 ** retries);
  return ceiling / 2 + (Math.random() * ceiling) / 2;
}

export interface StreamHandlers {
  /** By event name. A frame makes the stream live unless its handler returns an invalid-frame reason. */
  events: Record<string, (message: MessageEvent<string>) => string | void>;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
}

export type JsonFrame<T> = { success: true; data: T } | { success: false; error: string };

/** Parse one JSON event against a generated runtime schema, keeping parse failures explicit. */
export function parseJsonFrame<T>(schema: z.ZodType<T>, data: string, label: string): JsonFrame<T> {
  let value: unknown;
  try {
    value = JSON.parse(data) as unknown;
  } catch {
    return { success: false, error: `Invalid ${label} frame: malformed JSON` };
  }
  const parsed = schema.safeParse(value);
  if (parsed.success) return { success: true, data: parsed.data };
  const issue = parsed.error.issues[0];
  const path = issue?.path.length ? ` at ${issue.path.map(String).join(".")}` : "";
  return { success: false, error: `Invalid ${label} frame${path}: ${issue?.message ?? "schema mismatch"}` };
}

/** Follows `url` until the returned function is called. */
export function followStream(url: string, { events, onConnection }: StreamHandlers): () => void {
  let connection: StreamConnection = { phase: "connecting", since: Date.now() };
  let source: EventSource;
  let retries = 0;
  let retry: number | undefined;
  const stopped = new AbortController();

  function report(next: StreamConnection): void {
    connection = next;
    onConnection(next);
  }

  function open(): void {
    window.clearTimeout(retry);
    const current = (source = new EventSource(url));
    for (const [name, handle] of Object.entries(events))
      current.addEventListener(name, (message) => {
        const invalidReason = handle(message as MessageEvent<string>);
        if (invalidReason !== undefined) {
          report({
            phase: "invalid",
            since: connection.phase === "invalid" ? connection.since : Date.now(),
            lastError: invalidReason,
          });
          return;
        }
        if (connection.phase !== "live") {
          retries = 0;
          report({ phase: "live", since: Date.now() });
        }
      });
    current.addEventListener("error", () => {
      report({
        phase: "reconnecting",
        since: connection.phase === "live" ? Date.now() : connection.since,
        attempt: connection.phase === "reconnecting" ? connection.attempt + 1 : 1,
        lastError: null,
      });
      if (current.readyState !== EventSource.CLOSED) return;
      // The API client never settles a 401 but sends the browser to log in, so a source refused for
      // want of a login is not opened again. Any other answer, or none, leaves the backoff to decide.
      const probed = api.GET("/models", { signal: stopped.signal }).then(
        () => undefined,
        () => undefined
      );
      retry = window.setTimeout(
        () =>
          void probed.then(() => {
            if (source === current && !stopped.signal.aborted) open();
          }),
        backoff(retries++)
      );
    });
  }

  function resume(): void {
    if (source.readyState === EventSource.CLOSED) open();
  }

  function resumeInView(): void {
    if (document.visibilityState === "visible") resume();
  }

  report(connection);
  window.addEventListener("online", resume);
  document.addEventListener("visibilitychange", resumeInView);
  open();
  return () => {
    stopped.abort();
    window.clearTimeout(retry);
    window.removeEventListener("online", resume);
    document.removeEventListener("visibilitychange", resumeInView);
    source.close();
  };
}

export interface SnapshotStream<T> {
  /** The latest snapshot, or null before the first; a local write stands until the next frame. */
  snapshot: T | null;
  setSnapshot: Dispatch<SetStateAction<T | null>>;
  connection: StreamConnection;
}

/** Follows the `snapshot` frames of `url`, while it is not null. */
export function useSnapshotStream<T>(url: string | null): SnapshotStream<T> {
  const [snapshot, setSnapshot] = useState<T | null>(null);
  const [connection, setConnection] = useState<StreamConnection>(() => ({ phase: "connecting", since: Date.now() }));
  useEffect(() => {
    if (url === null) return;
    return followStream(url, {
      events: { snapshot: (message) => setSnapshot(JSON.parse(message.data) as T) },
      onConnection: setConnection,
    });
  }, [url]);
  return { snapshot, setSnapshot, connection };
}
