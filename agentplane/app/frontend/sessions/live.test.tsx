// @vitest-environment happy-dom
import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { WatchSessionSummariesResponseSchema } from "../../../history_service/api/history/history_service_pb";
import { act, type JSX } from "react";
import { afterEach, expect, it, vi } from "vitest";
import {
  SessionSchema,
  WatchSessionsResponseSchema,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { cleanupMounted, mountWithMantine } from "../../../../util/frontend/testing/react";
import { sessionDispatcherClient } from "../transport/session_dispatcher_client";
import { followWatch, type WatchHandlers } from "../transport/watch_stream";
import { listThreadMetadata } from "./client";
import { threadView } from "./testing/state";
import { combinedConnection, useThreadsLive } from "./live";
import type * as ClientModule from "./client";

it.each([
  {
    history: "the harness dropped this stream",
    metadata: "the harness dropped this stream",
    expected: "the harness dropped this stream",
  },
  {
    history: "HTTP 503",
    metadata: "stream stopped sending frames",
    expected: "HTTP 503; stream stopped sending frames",
  },
])("names each distinct reason the watches dropped for once: $expected", ({ history, metadata, expected }) => {
  const combined = combinedConnection(
    { phase: "reconnecting", since: 2_000, attempt: 1, lastError: history },
    { phase: "reconnecting", since: 1_000, attempt: 3, lastError: metadata },
    { phase: "live", since: 0 }
  );
  expect(combined).toEqual({ phase: "reconnecting", since: 1_000, attempt: 3, lastError: expected });
});

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof ClientModule>()),
  listThreadMetadata: vi.fn(),
}));
vi.mock("../transport/session_dispatcher_client", () => ({ sessionDispatcherClient: { watchSessions: vi.fn() } }));
vi.mock("../transport/watch_stream", () => ({ followWatch: vi.fn() }));

afterEach(async () => {
  await cleanupMounted();
  vi.resetAllMocks();
});

/**
 * Mounts a probe of `useThreadsLive` over mocked watches, once the inventory, App metadata and selected
 * summaries watches have delivered one Thread. The probe shows what `read` makes of the live Threads.
 */
async function liveThread(read: (live: ReturnType<typeof useThreadsLive>) => string) {
  let inventory!: Parameters<typeof sessionDispatcherClient.watchSessions>[0];
  const watches = new Map<string, WatchHandlers>();
  vi.mocked(sessionDispatcherClient.watchSessions).mockImplementation((handlers) => {
    inventory = handlers;
    return () => {};
  });
  vi.mocked(followWatch).mockImplementation((path, handlers) => {
    watches.set(String(path), handlers);
    return () => {};
  });
  const value = threadView();
  const observed = create(SessionSchema, { sessionId: value.metadata.threadId, createdAt: value.metadata.createdAt });
  vi.mocked(listThreadMetadata).mockResolvedValue(new Map([[value.metadata.threadId, value.metadata]]));
  const listing = { limit: 50, search: null, includeArchived: false };
  function Probe(): JSX.Element {
    return <span>{read(useThreadsLive(listing, null))}</span>;
  }
  const { container } = await mountWithMantine(<Probe />);
  await act(async () => {
    inventory.onOpen(true);
    inventory.onConnection({ phase: "live", since: 0 });
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "session", value: observed } }));
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "caughtUp", value: {} } }));
    watches.get("/upstream/app/v1/threads:watch")!.onFrame({ changed: {}, updatesConnected: true });
  });
  const selected = [...watches.entries()].find(([key]) => key.includes("selected.session_ids="))![1];
  const sendSummary = (observation: MessageInitShape<typeof WatchSessionSummariesResponseSchema>["observation"]) =>
    act(async () =>
      selected.onFrame(
        toJson(WatchSessionSummariesResponseSchema, create(WatchSessionSummariesResponseSchema, { observation }))
      )
    );
  await sendSummary({ case: "snapshot", value: { summaries: [value.summary] } });
  return { probe: container.querySelector("span")!, inventory, sendSummary, value };
}

it("updates watched summaries without fetching metadata again", async () => {
  const { probe, sendSummary, value } = await liveThread((live) =>
    String(live.snapshot?.threads[0]?.summary.foldedThrough ?? "missing")
  );
  await sendSummary({ case: "summary", value: { ...value.summary, foldedThrough: 123n } });
  expect(probe.textContent).toBe("123");
  expect(listThreadMetadata).toHaveBeenCalledTimes(1);
});

it("marks Dispatcher activity stale when the inventory watch reconnects", async () => {
  const { probe, inventory } = await liveThread((live) => String(live.snapshot?.activity_current));
  expect(probe.textContent).toBe("true");
  await act(async () =>
    inventory.onConnection({ phase: "reconnecting", since: 0, attempt: 1, lastError: "disconnected" })
  );
  expect(probe.textContent).toBe("false");
});
