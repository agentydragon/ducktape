/**
 * A streamed HTTP response, followed for as long as a page needs it: the one reader under the app's
 * event streams (`live_stream.ts`) and service watches (`watch_stream.ts`), which differ only in how
 * the body splits into frames.
 *
 * The request goes through `fetchWithLogin`, so a 401 sends the browser to log in and nothing is
 * retried. A connection that ends for any other reason -- a refusal, a dropped network, a malformed
 * frame, the server closing the body -- is replaced by a new one after a backoff, sooner once the
 * browser is back online or the page is back in view. Neither framing carries a resume position: each
 * connection starts from the server's current state.
 */
import { displayableError, fetchWithLogin, HttpError } from "./client";

/** `since` is when the stream entered the phase; `reconnecting` covers every failure since it was
 * last live, a failed first connection included, and `attempt` counts them. `lastError` is why the
 * last one failed, or null when the server ended the response. */
export type StreamConnection =
  | { phase: "connecting"; since: number }
  | { phase: "live"; since: number }
  | { phase: "reconnecting"; since: number; attempt: number; lastError: string | null };

const FIRST_RETRY_MS = 1_000;
const LAST_RETRY_MS = 30_000;

/** Between half and all of a delay that doubles with each retry, so the pages that lost one server
 * do not all come back to it at once. */
export function backoff(retries: number): number {
  const ceiling = Math.min(LAST_RETRY_MS, FIRST_RETRY_MS * 2 ** retries);
  return ceiling / 2 + (Math.random() * ceiling) / 2;
}

export interface ReaderHandlers<F> {
  /** The `Accept` header of each request. */
  accept: string;
  /** Splits the decoded body into frames; erroring it ends the connection. */
  frames: (body: ReadableStream<string>) => ReadableStream<F>;
  /** A response was accepted: the frames that follow start from the server's current state. */
  onOpen?: () => void;
  /** One frame, which makes the stream live. Throwing ends the connection. */
  onFrame: (frame: F) => void;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
}

/** Follows `url` until the returned function is called. */
export function followResponse<F>(
  url: string,
  { accept, frames, onOpen, onFrame, onConnection }: ReaderHandlers<F>
): () => void {
  let connection: StreamConnection = { phase: "connecting", since: Date.now() };
  let retries = 0;
  let retry: number | undefined;
  let closed = false;
  // The request in flight, null while a backoff runs.
  let current: AbortController | null = null;

  function report(next: StreamConnection): void {
    connection = next;
    onConnection(next);
  }

  async function read(signal: AbortSignal): Promise<void> {
    const response = await fetchWithLogin(url, { signal, headers: { Accept: accept } });
    if (!response.ok) throw new HttpError(response, await response.text());
    if (response.body === null) throw new Error("the response has no body");
    onOpen?.();
    const reader = frames(response.body.pipeThrough(new TextDecoderStream())).getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      if (connection.phase !== "live") {
        retries = 0;
        report({ phase: "live", since: Date.now() });
      }
      onFrame(value);
    }
  }

  async function connect(): Promise<void> {
    window.clearTimeout(retry);
    const attempt = (current = new AbortController());
    let lastError: string | null = null;
    try {
      await read(attempt.signal);
    } catch (reason) {
      lastError = displayableError(reason);
    } finally {
      // A malformed frame or a consumer error must close the old body before reconnecting.
      attempt.abort();
    }
    if (closed) return;
    current = null;
    report({
      phase: "reconnecting",
      since: connection.phase === "live" ? Date.now() : connection.since,
      attempt: connection.phase === "reconnecting" ? connection.attempt + 1 : 1,
      lastError,
    });
    retry = window.setTimeout(() => void connect(), backoff(retries++));
  }

  function resume(): void {
    if (current === null && !closed) void connect();
  }

  function resumeInView(): void {
    if (document.visibilityState === "visible") resume();
  }

  report(connection);
  window.addEventListener("online", resume);
  document.addEventListener("visibilitychange", resumeInView);
  void connect();
  return () => {
    closed = true;
    window.clearTimeout(retry);
    window.removeEventListener("online", resume);
    document.removeEventListener("visibilitychange", resumeInView);
    current?.abort();
  };
}
