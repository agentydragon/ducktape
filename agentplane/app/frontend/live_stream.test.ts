// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { followStream } from "./live_stream";
import type { StreamConnection } from "./stream_reader";
import { type ServedWatch, stubWatchFetch } from "./testing/watch";

let fetchMock: ReturnType<typeof stubWatchFetch>;
let events: [string, string][] = [];
let connections: StreamConnection[] = [];
let stop: () => void = () => undefined;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = stubWatchFetch();
  stop = followStream("/test/stream", {
    events: {
      snapshot: (message) => events.push(["snapshot", message.data]),
      message: (message) => events.push(["message", message.data]),
    },
    onConnection: (connection) => connections.push(connection),
  });
});

afterEach(() => {
  stop();
  events = [];
  connections = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function stream(): Promise<ServedWatch> {
  await vi.advanceTimersByTimeAsync(0);
  const served = fetchMock.served[0];
  if (served === undefined) throw new Error("no stream requested");
  return served;
}

it("asks for an event stream", async () => {
  await stream();
  expect(new Headers(fetchMock.fetch.mock.calls[0]?.[1].headers).get("Accept")).toBe("text/event-stream");
});

it("hands each event to the handler of its name, however the bytes are split", async () => {
  const served = await stream();
  served.write('event: snapshot\ndata: {"n":');
  await vi.advanceTimersByTimeAsync(0);
  expect(events).toEqual([]);
  served.write('1}\n\ndata: unnamed\n\n: a comment\n\nevent: snapshot\ndata: {"n":2}\n\n');
  await vi.advanceTimersByTimeAsync(0);

  expect(events).toEqual([
    ["snapshot", '{"n":1}'],
    ["message", "unnamed"],
    ["snapshot", '{"n":2}'],
  ]);
});

it("ignores events it has no handler for, which do not make the stream live", async () => {
  const served = await stream();
  served.event("unknown", "{}");
  await vi.advanceTimersByTimeAsync(0);
  expect(connections.map(({ phase }) => phase)).toEqual(["connecting"]);

  served.event("snapshot", "{}");
  await vi.advanceTimersByTimeAsync(0);
  expect(connections.map(({ phase }) => phase)).toEqual(["connecting", "live"]);
  expect(events).toEqual([["snapshot", "{}"]]);
});
