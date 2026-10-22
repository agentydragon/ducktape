/**
 * A server-streaming RPC of a service's generated HTTP/JSON facade, followed for as long as a page
 * needs it. The facade answers with newline-delimited JSON: one `{"result": ...}` object per message,
 * and a last `{"error": ...}` object when the RPC fails after it began.
 *
 * A watch replays what it holds on every connection and carries no resume position, so a connection
 * that ended for any reason is replaced by a new one after a backoff, sooner once the browser is back
 * online or the page is back in view. `onOpen` marks where a replay begins.
 */
import { displayableError, fetchWithLogin, HttpError } from "./client";
import { backoff, type StreamConnection } from "./live_stream";

export interface WatchHandlers {
  /** A response was accepted: the frames that follow replay the watch's state from its start. */
  onOpen: () => void;
  /** The `result` of one message. Throwing ends the connection. */
  onFrame: (result: unknown) => void;
  /** Called with the opening `connecting` at once, then on every change. */
  onConnection: (connection: StreamConnection) => void;
}

interface Message {
  result?: unknown;
  error?: { code: number; message: string };
}

/** Follows `url` until the returned function is called. */
export function followWatch(url: string, { onOpen, onFrame, onConnection }: WatchHandlers): () => void {
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

  function receive(line: string): void {
    const message = JSON.parse(line) as Message;
    if (message.error !== undefined) throw new Error(`${message.error.message} (code ${message.error.code})`);
    if (connection.phase !== "live") {
      retries = 0;
      report({ phase: "live", since: Date.now() });
    }
    onFrame(message.result);
  }

  async function read(signal: AbortSignal): Promise<void> {
    const response = await fetchWithLogin(url, { signal, headers: { Accept: "application/x-ndjson" } });
    if (!response.ok) throw new HttpError(response, await response.text());
    if (response.body === null) throw new Error("the response has no body");
    onOpen();
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let partial = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      const lines = (partial + value).split("\n");
      partial = lines.pop() ?? "";
      for (const line of lines) if (line !== "") receive(line);
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
