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
import { api } from "./client";

/** `since` is when the stream entered the phase; `reconnecting` covers every failure since it was
 * last live, a failed first connection included, and `attempt` counts them. */
export type StreamConnection =
  | { phase: "connecting"; since: number }
  | { phase: "live"; since: number }
  | { phase: "reconnecting"; since: number; attempt: number };

const FIRST_RETRY_MS = 1_000;
const LAST_RETRY_MS = 30_000;

/** Between half and all of a delay that doubles with each retry, so the pages that lost one server
 * do not all come back to it at once. */
function backoff(retries: number): number {
  const ceiling = Math.min(LAST_RETRY_MS, FIRST_RETRY_MS * 2 ** retries);
  return ceiling / 2 + (Math.random() * ceiling) / 2;
}

export interface StreamHandlers {
  /** By event name. A frame of any of them is what makes the stream live. */
  events: Record<string, (message: MessageEvent<string>) => void>;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
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
        if (connection.phase !== "live") {
          retries = 0;
          report({ phase: "live", since: Date.now() });
        }
        handle(message as MessageEvent<string>);
      });
    current.addEventListener("error", () => {
      report({
        phase: "reconnecting",
        since: connection.phase === "live" ? Date.now() : connection.since,
        attempt: connection.phase === "reconnecting" ? connection.attempt + 1 : 1,
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
