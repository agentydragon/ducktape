// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { followStream, type StreamConnection } from "./live_stream";

// The API client takes `fetch` when it is created, so the stub is in place before it is imported.
const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(request: Request) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

/** Fails as the browser's does: back to `CONNECTING` on a dropped network, `CLOSED` on a response
 * that is not a stream. */
class FakeSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = FakeSource.CONNECTING;

  constructor(url: string) {
    super();
    expect(url).toBe("/test/stream");
    sources.push(this);
  }

  close(): void {
    this.readyState = FakeSource.CLOSED;
  }

  frame(): void {
    this.readyState = FakeSource.OPEN;
    this.dispatchEvent(new MessageEvent("snapshot", { data: "{}" }));
  }

  fail(readyState: number): void {
    this.readyState = readyState;
    this.dispatchEvent(new Event("error"));
  }
}

let sources: FakeSource[] = [];
let connections: StreamConnection[] = [];
let stop: () => void = () => undefined;

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", FakeSource);
  fetchMock.mockReset().mockImplementation(async () => Response.json({}));
});

afterEach(() => {
  stop();
  sources = [];
  connections = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function follow(): void {
  stop = followStream("/test/stream", {
    events: { snapshot: () => undefined },
    onConnection: (connection) => connections.push(connection),
  });
}

function latest(): FakeSource {
  const source = sources.at(-1);
  if (source === undefined) throw new Error("no stream opened");
  return source;
}

it("reports when the stream stopped being live and how often it has failed since", () => {
  const start = Date.now();
  follow();
  vi.advanceTimersByTime(1_000);
  latest().fail(FakeSource.CONNECTING);
  latest().frame();
  vi.advanceTimersByTime(2_000);
  latest().fail(FakeSource.CONNECTING);
  vi.advanceTimersByTime(1_000);
  latest().fail(FakeSource.CONNECTING);
  expect(connections).toEqual([
    { phase: "connecting", since: start },
    // A first connection that fails is down from when it began.
    { phase: "reconnecting", since: start, attempt: 1 },
    { phase: "live", since: start + 1_000 },
    { phase: "reconnecting", since: start + 3_000, attempt: 1 },
    { phase: "reconnecting", since: start + 3_000, attempt: 2 },
  ]);
  // The browser retries a dropped network itself.
  expect(sources).toHaveLength(1);
  expect(fetchMock).not.toHaveBeenCalled();
});

it("sends the browser to log in when a refused stream's login has expired, and opens it no more", async () => {
  const replace = vi.fn();
  vi.stubGlobal("location", { pathname: "/", hash: "#/actions", replace });
  fetchMock.mockImplementation(async () => Response.json({ detail: "test session expired" }, { status: 401 }));
  follow();
  latest().fail(FakeSource.CLOSED);
  await vi.advanceTimersByTimeAsync(60_000);
  expect(replace.mock.calls).toEqual([["/auth/login"]]);
  expect(sources).toHaveLength(1);
});

it("reopens a refused stream after a backoff that doubles, and starts it over once a frame arrives", async () => {
  follow();
  // Each delay is between half and all of its ceiling.
  for (const ceiling of [1_000, 2_000, 4_000]) {
    const opened = sources.length;
    latest().fail(FakeSource.CLOSED);
    await vi.advanceTimersByTimeAsync(ceiling / 2 - 1);
    expect(sources).toHaveLength(opened);
    await vi.advanceTimersByTimeAsync(ceiling / 2 + 1);
    expect(sources).toHaveLength(opened + 1);
  }
  expect(fetchMock).toHaveBeenCalledTimes(3);

  latest().frame();
  latest().fail(FakeSource.CLOSED);
  await vi.advanceTimersByTimeAsync(1_000);
  expect(sources).toHaveLength(5);
});

it("reopens a refused stream at once when the browser is back online or the page back in view", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  follow();
  latest().fail(FakeSource.CONNECTING);
  window.dispatchEvent(new Event("online"));
  // The browser is already retrying that one.
  expect(sources).toHaveLength(1);

  latest().fail(FakeSource.CLOSED);
  window.dispatchEvent(new Event("online"));
  expect(sources).toHaveLength(2);

  latest().fail(FakeSource.CLOSED);
  document.dispatchEvent(new Event("visibilitychange"));
  expect(sources).toHaveLength(2);
  visibility.mockReturnValue("visible");
  document.dispatchEvent(new Event("visibilitychange"));
  expect(sources).toHaveLength(3);

  // Nor do the backoffs those cut short open anything later.
  await vi.advanceTimersByTimeAsync(60_000);
  expect(sources).toHaveLength(3);
});

it("does not reopen a stream that came back while its probe was out", async () => {
  let answer: (response: Response) => void = () => undefined;
  fetchMock.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
  follow();
  latest().fail(FakeSource.CLOSED);
  // The backoff runs out with the probe unanswered, and the browser comes back online meanwhile.
  await vi.advanceTimersByTimeAsync(1_000);
  window.dispatchEvent(new Event("online"));
  latest().frame();
  answer(Response.json({}));
  await vi.advanceTimersByTimeAsync(60_000);
  expect(sources).toHaveLength(2);
  expect(latest().readyState).toBe(FakeSource.OPEN);
});

it("opens nothing once stopped, whatever it was waiting on", async () => {
  let answer: (response: Response) => void = () => undefined;
  fetchMock.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
  follow();
  latest().fail(FakeSource.CLOSED);
  await vi.advanceTimersByTimeAsync(1_000);
  stop();
  answer(Response.json({}));
  window.dispatchEvent(new Event("online"));
  await vi.advanceTimersByTimeAsync(60_000);
  expect(sources).toHaveLength(1);

  follow();
  latest().frame();
  stop();
  expect(latest().readyState).toBe(FakeSource.CLOSED);
});
