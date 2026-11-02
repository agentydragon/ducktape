// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { decodeNotificationWatchFrame } from "./notification_watch";
import { followWatch } from "./watch_stream";
import { stubWatchFetch } from "./testing/watch";

let stop: () => void = () => {};

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("rejects renewal before the first snapshot, retries, then renews immediately after a snapshot", async () => {
  const fetchMock = stubWatchFetch();
  vi.spyOn(Math, "random").mockReturnValue(1);
  let sawSnapshot = false;
  const connections: string[] = [];
  stop = followWatch("/test/notifications:watch", {
    onOpen: () => {
      sawSnapshot = false;
    },
    onFrame: (json) => {
      const frame = decodeNotificationWatchFrame(json, sawSnapshot);
      if (frame.kind === "snapshot") sawSnapshot = true;
      else return "reconnect";
    },
    onConnection: (connection) => connections.push(connection.phase),
  });
  await vi.advanceTimersByTimeAsync(0);
  fetchMock.served[0]?.send({ reconnectRequired: {} });
  await vi.advanceTimersByTimeAsync(0);

  expect(fetchMock.served[0]?.signal?.aborted).toBe(true);
  expect(fetchMock.fetch).toHaveBeenCalledTimes(1);
  expect(connections.at(-1)).toBe("reconnecting");
  await vi.advanceTimersByTimeAsync(999);
  expect(fetchMock.fetch).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);

  fetchMock.served[1]?.send({ snapshot: { observedAt: "2025-02-01T12:00:00Z", inboxes: [] } });
  await vi.advanceTimersByTimeAsync(0);
  expect(sawSnapshot).toBe(true);
  fetchMock.served[1]?.send({ reconnectRequired: {} });
  await vi.advanceTimersByTimeAsync(0);

  expect(fetchMock.served[1]?.signal?.aborted).toBe(true);
  expect(fetchMock.fetch).toHaveBeenCalledTimes(3);
});
