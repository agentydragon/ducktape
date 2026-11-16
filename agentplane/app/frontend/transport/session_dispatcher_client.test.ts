// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { sessionDispatcherClient } from "./session_dispatcher_client";
import { stubWatchFetch } from "../testing/watch";

let fetchMock: ReturnType<typeof stubWatchFetch>;
let stop: () => void = () => undefined;
const opened = vi.fn();
const applied = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = stubWatchFetch();
});

afterEach(() => {
  stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function follow(): void {
  stop = sessionDispatcherClient.watchSessions({
    onOpen: opened,
    onFrame: applied,
    onConnection: () => undefined,
  });
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

it("resumes from the last applied token and keeps partial snapshots across disconnects", async () => {
  follow();
  await settle();
  const first = fetchMock.served[0]!;
  expect(first.url.pathname).toBe("/upstream/dispatcher/v1/sessions:watch");
  expect(first.url.searchParams.get("resume_token")).toBe("");
  first.send({ session: { sessionId: "test-first-session" }, resumeToken: "test-first-token" });
  await settle();
  first.drop();
  await vi.advanceTimersByTimeAsync(1_000);
  const resumed = fetchMock.served[1]!;
  expect(resumed.url.searchParams.get("resume_token")).toBe("test-first-token");
  resumed.send({ caughtUp: {}, resumeToken: "test-caught-up-token" });
  await settle();
  expect(opened.mock.calls).toEqual([[true], [false]]);
  expect(applied.mock.calls.map(([frame]) => frame.frame.case)).toEqual(["session", "caughtUp"]);
  resumed.send({ reconnectRequired: {}, resumeToken: "test-caught-up-token" });
  resumed.end();
  await vi.advanceTimersByTimeAsync(1_000);
  expect(fetchMock.served[2]!.url.searchParams.get("resume_token")).toBe("test-caught-up-token");
});

it.each(["http", "stream"])("clears a token rejected with INVALID_ARGUMENT over %s", async (mode) => {
  follow();
  await settle();
  fetchMock.served[0]!.send({ caughtUp: {}, resumeToken: "test-rejected-token" });
  await settle();
  if (mode === "http") {
    fetchMock.fetch.mockResolvedValueOnce(
      Response.json({ code: 3, message: "test token scope changed" }, { status: 400 })
    );
    fetchMock.served[0]!.end();
  } else {
    fetchMock.served[0]!.write('{"error":{"code":3,"message":"test token scope changed"}}\n');
  }
  await vi.advanceTimersByTimeAsync(3_000);
  const latest = fetchMock.served.at(-1)!;
  expect(latest.url.searchParams.get("resume_token")).toBe("");
  expect(opened.mock.calls.at(-1)).toEqual([true]);
});

it("does not advance the checkpoint past a consumer failure", async () => {
  follow();
  await settle();
  const first = fetchMock.served[0]!;
  first.send({ caughtUp: {}, resumeToken: "test-applied-token" });
  await settle();
  applied.mockImplementationOnce(() => {
    throw new Error("test local apply failed");
  });
  first.send({ session: { sessionId: "test-unapplied-session" }, resumeToken: "test-unapplied-token" });
  await vi.advanceTimersByTimeAsync(1_000);
  expect(fetchMock.served[1]!.url.searchParams.get("resume_token")).toBe("test-applied-token");
});

it("starts a new watch without a previous browser lifetime's token", async () => {
  follow();
  await settle();
  fetchMock.served[0]!.send({ caughtUp: {}, resumeToken: "test-previous-lifetime-token" });
  await settle();
  stop();
  follow();
  await settle();
  expect(fetchMock.served[1]!.url.searchParams.get("resume_token")).toBe("");
});
