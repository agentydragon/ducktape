// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { StreamConnection } from "./stream_reader";
import { followWatch } from "./watch_stream";
import { type ServedWatch, stubWatchFetch } from "./testing/watch";

let fetchMock: ReturnType<typeof stubWatchFetch>;
let events: unknown[] = [];
let connections: StreamConnection[] = [];
let stop: () => void = () => undefined;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = stubWatchFetch();
});

afterEach(() => {
  stop();
  events = [];
  connections = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function follow(): void {
  stop = followWatch("/test/watch", {
    onOpen: () => events.push("open"),
    onFrame: (result) => events.push(result),
    onConnection: (connection) => connections.push(connection),
  });
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function connection(): StreamConnection {
  const latest = connections.at(-1);
  if (latest === undefined) throw new Error("no connection reported");
  return latest;
}

function watch(index: number): ServedWatch {
  const served = fetchMock.served[index];
  if (served === undefined) throw new Error(`request ${index} was not made`);
  return served;
}

it("hands over each result as it arrives, however the bytes are split", async () => {
  follow();
  await settle();
  watch(0).write('{"result":{"n":1}}\n{"res');
  await settle();
  expect(events).toEqual(["open", { n: 1 }]);
  watch(0).write('ult":{"n":2}}\n');
  await settle();

  expect(events).toEqual(["open", { n: 1 }, { n: 2 }]);
  expect(connections.map(({ phase }) => phase)).toEqual(["connecting", "live"]);
});

it("opens a new connection after one ends, and says the replay starts again", async () => {
  follow();
  await settle();
  watch(0).send(1);
  watch(0).end();
  await settle();
  expect(connection()).toMatchObject({ phase: "reconnecting", attempt: 1, lastError: null });

  await vi.advanceTimersByTimeAsync(1_000);
  watch(1).send(2);
  await settle();

  expect(connection().phase).toBe("live");
  expect(events).toEqual(["open", 1, "open", 2]);
});

it("treats an error message of the stream as the end of the connection", async () => {
  follow();
  await settle();
  watch(0).write('{"error":{"code":14,"message":"lease lost"}}\n');
  await settle();

  expect(connection()).toMatchObject({ phase: "reconnecting", lastError: expect.stringContaining("lease lost") });
  expect(watch(0).signal?.aborted).toBe(true);
});
