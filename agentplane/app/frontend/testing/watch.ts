import { vi } from "vitest";

/** One streamed response, which a test writes into and ends. */
export class ServedWatch {
  signal: AbortSignal | undefined;
  readonly url: URL;
  /** Whether the test dropped it. */
  dropped = false;
  private controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  readonly response: Response = new Response(
    new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
      // The reader has taken everything written so far.
      pull: () => {
        if (this.dropped) this.fail();
      },
      cancel: () => {
        this.controller = undefined;
      },
    })
  );

  constructor(url: string, signal: AbortSignal | undefined) {
    this.url = new URL(url, "http://test");
    this.signal = signal;
    // As the browser's: an aborted request fails its body.
    signal?.addEventListener(
      "abort",
      () => {
        this.controller?.error(signal.reason);
        this.controller = undefined;
      },
      { once: true }
    );
  }

  write(text: string): void {
    this.controller?.enqueue(new TextEncoder().encode(text));
  }

  /** One message as the facade writes it: `{"result": ...}` and a newline. */
  send(result: unknown): void {
    this.write(`${JSON.stringify({ result })}\n`);
  }

  /** One server-sent event, its `data` as given. */
  event(name: string, data: string): void {
    this.write(`event: ${name}\ndata: ${data}\n\n`);
  }

  end(): void {
    this.controller?.close();
    this.controller = undefined;
  }

  /** Fails the body as a dropped network does, once the reader has taken what was written before. */
  drop(): void {
    this.dropped = true;
    // Erroring a stream discards what it still holds.
    if ((this.controller?.desiredSize ?? 0) > 0) this.fail();
  }

  private fail(): void {
    this.controller?.error(new TypeError("test stream dropped"));
    this.controller = undefined;
  }
}

/** Stubs `fetch` so that each request is answered with an open watch, collected in `served` in the
 * order of the requests. */
export function stubWatchFetch(): { served: ServedWatch[]; fetch: ReturnType<typeof vi.fn> } {
  const served: ServedWatch[] = [];
  const fetch = vi.fn((url: string, init: RequestInit) => {
    const watch = new ServedWatch(url, init.signal ?? undefined);
    served.push(watch);
    return Promise.resolve(watch.response);
  });
  vi.stubGlobal("fetch", fetch);
  return { served, fetch };
}

function accepts(init: RequestInit | undefined, type: string): boolean {
  return new Headers(init?.headers).get("Accept") === type;
}

/**
 * Stubs `fetch` so that each `text/event-stream` request opens a stream, handed to `open` and
 * collected in the returned list in the order of the requests; any other request goes to `other`.
 */
export function serveEventStreams(
  open: (stream: ServedWatch) => void,
  other: typeof fetch = globalThis.fetch
): ServedWatch[] {
  const served: ServedWatch[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input !== "string" || !accepts(init, "text/event-stream")) return other(input, init);
    const stream = new ServedWatch(input, init?.signal ?? undefined);
    served.push(stream);
    open(stream);
    return Promise.resolve(stream.response);
  });
  return served;
}

/** Whether an earlier stream on the same URL as `stream` was dropped: the server a test models
 * answers such a reconnection, but says nothing until the test does. */
export function reconnected(served: readonly ServedWatch[], stream: ServedWatch): boolean {
  return served.some((earlier) => earlier !== stream && earlier.url.href === stream.url.href && earlier.dropped);
}

/** The latest stream opened on `pathname`. */
export function latestOn(served: readonly ServedWatch[], pathname: string): ServedWatch {
  const stream = served.filter((candidate) => candidate.url.pathname === pathname).at(-1);
  if (stream === undefined) throw new Error(`no stream opened on ${pathname}`);
  return stream;
}
