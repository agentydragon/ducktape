// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { followResponse, type StreamConnection } from "./stream_reader";
import { type ServedWatch, stubWatchFetch } from "./testing/watch";

let fetchMock: ReturnType<typeof stubWatchFetch>;
let frames: string[] = [];
let connections: StreamConnection[] = [];
let stop: () => void = () => undefined;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = stubWatchFetch();
});

afterEach(() => {
  stop();
  frames = [];
  connections = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** Follows a test stream whose every chunk of text is one frame. */
function follow(): void {
  stop = followResponse("/test/stream", {
    accept: "text/plain",
    frames: (body) => body,
    onFrame: (frame) => {
      frames.push(frame);
    },
    onConnection: (connection) => connections.push(connection),
  });
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function served(index: number): ServedWatch {
  const stream = fetchMock.served[index];
  if (stream === undefined) throw new Error(`request ${index} was not made`);
  return stream;
}

it("reports when the stream stopped being live and how often it has failed since", async () => {
  const start = Date.now();
  follow();
  await settle();
  await vi.advanceTimersByTimeAsync(1_000);
  served(0).drop();
  await settle();
  // The backoff after a first failure is under a second.
  await vi.advanceTimersByTimeAsync(1_000);
  served(1).write("frame");
  await settle();
  await vi.advanceTimersByTimeAsync(2_000);
  served(1).end();
  await settle();
  await vi.advanceTimersByTimeAsync(1_000);
  served(2).drop();
  await settle();

  expect(connections).toEqual([
    { phase: "connecting", since: start },
    // A first connection that fails is down from when it began.
    { phase: "reconnecting", since: start, attempt: 1, lastError: "test stream dropped" },
    { phase: "live", since: expect.any(Number) },
    // A body the server ended carries no error.
    { phase: "reconnecting", since: expect.any(Number), attempt: 1, lastError: null },
    { phase: "reconnecting", since: connections[3]?.since, attempt: 2, lastError: "test stream dropped" },
  ]);
  expect(frames).toEqual(["frame"]);
});

it("sends the browser to log in when the login has expired, and connects no more", async () => {
  const replace = vi.fn();
  vi.stubGlobal("location", { pathname: "/", hash: "#/actions", replace });
  fetchMock.fetch.mockResolvedValue(Response.json({ detail: "test session expired" }, { status: 401 }));
  follow();
  await vi.advanceTimersByTimeAsync(60_000);

  expect(replace.mock.calls).toEqual([["/auth/login"]]);
  expect(fetchMock.fetch).toHaveBeenCalledOnce();
  expect(connections.map(({ phase }) => phase)).toEqual(["connecting"]);
});

it("reports why a request was refused, and retries after a backoff that doubles until a frame arrives", async () => {
  // The first four connections are refused, the fifth answered.
  for (let refused = 0; refused < 4; refused++)
    fetchMock.fetch.mockImplementationOnce(() => Promise.resolve(new Response("test refusal", { status: 503 })));
  // The longest jitter, so each delay is its whole ceiling.
  vi.spyOn(Math, "random").mockReturnValue(1);
  follow();
  await settle();
  expect(connections.at(-1)).toMatchObject({ phase: "reconnecting", lastError: expect.stringContaining("503") });
  for (const ceiling of [1_000, 2_000, 4_000, 8_000]) {
    const made = fetchMock.fetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(ceiling - 1);
    expect(fetchMock.fetch).toHaveBeenCalledTimes(made);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock.fetch).toHaveBeenCalledTimes(made + 1);
  }

  served(0).write("frame");
  await settle();
  expect(connections.at(-1)?.phase).toBe("live");
  served(0).end();
  await settle();
  // A frame starts the backoff over.
  await vi.advanceTimersByTimeAsync(1_000);
  expect(fetchMock.fetch).toHaveBeenCalledTimes(6);
});

it("connects again at once when the browser is back online or the page back in view", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  follow();
  await settle();
  // A connection in flight is not doubled.
  window.dispatchEvent(new Event("online"));
  await settle();
  expect(fetchMock.fetch).toHaveBeenCalledTimes(1);

  served(0).end();
  await settle();
  window.dispatchEvent(new Event("online"));
  await settle();
  expect(fetchMock.fetch).toHaveBeenCalledTimes(2);

  served(1).end();
  await settle();
  document.dispatchEvent(new Event("visibilitychange"));
  await settle();
  expect(fetchMock.fetch).toHaveBeenCalledTimes(2);
  visibility.mockReturnValue("visible");
  document.dispatchEvent(new Event("visibilitychange"));
  await settle();
  expect(fetchMock.fetch).toHaveBeenCalledTimes(3);

  // Nor do the backoffs those cut short connect again later.
  await vi.advanceTimersByTimeAsync(60_000);
  expect(fetchMock.fetch).toHaveBeenCalledTimes(3);
});

it("ends the connection when a frame's consumer throws, and reconnects", async () => {
  stop = followResponse("/test/stream", {
    accept: "text/plain",
    frames: (body) => body,
    onFrame: () => {
      throw new Error("test consumer failure");
    },
    onConnection: (connection) => connections.push(connection),
  });
  await settle();
  served(0).write("frame");
  await settle();

  expect(served(0).signal?.aborted).toBe(true);
  expect(connections.at(-1)).toMatchObject({ phase: "reconnecting", lastError: "test consumer failure" });
  await vi.advanceTimersByTimeAsync(1_000);
  expect(fetchMock.fetch).toHaveBeenCalledTimes(2);
});

it("aborts the request in flight when stopped, and connects nothing more", async () => {
  follow();
  await settle();
  stop();
  window.dispatchEvent(new Event("online"));
  await vi.advanceTimersByTimeAsync(60_000);

  expect(served(0).signal?.aborted).toBe(true);
  expect(fetchMock.fetch).toHaveBeenCalledOnce();
});
