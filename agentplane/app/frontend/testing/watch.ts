import { vi } from "vitest";

/** One response of a watch, which a test writes into and ends. */
export class ServedWatch {
  signal: AbortSignal | undefined;
  readonly url: URL;
  private controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  readonly response: Response = new Response(
    new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
    }),
    { headers: { "Content-Type": "application/x-ndjson" } }
  );

  constructor(url: string, signal: AbortSignal | undefined) {
    this.url = new URL(url, "http://test");
    this.signal = signal;
  }

  write(text: string): void {
    this.controller?.enqueue(new TextEncoder().encode(text));
  }

  /** One message as the facade writes it: `{"result": ...}` and a newline. */
  send(result: unknown): void {
    this.write(`${JSON.stringify({ result })}\n`);
  }

  end(): void {
    this.controller?.close();
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
