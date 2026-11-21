// @vitest-environment happy-dom
import { create, toJson } from "@bufbuild/protobuf";
import { WatchSelectedSessionSummariesResponseSchema } from "../../../history_service/api/history/history_service_pb";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import {
  SessionSchema,
  WatchSessionsResponseSchema,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
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

let root: ReturnType<typeof createRoot> | undefined;
let container: HTMLDivElement | undefined;
afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.resetAllMocks();
});

it("updates watched summaries without fetching metadata again", async () => {
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
  const id = value.metadata.threadId;
  const observed = create(SessionSchema, { sessionId: id, createdAt: value.metadata.createdAt });
  vi.mocked(listThreadMetadata).mockResolvedValue(new Map([[id, value.metadata]]));
  const listing = { limit: 50, search: null, includeArchived: false };
  function Probe(): JSX.Element {
    const live = useThreadsLive(listing, null);
    return <span data-cursor={String(live.snapshot?.threads[0]?.summary.foldedThrough ?? "missing")} />;
  }
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root!.render(<Probe />));
  await act(async () => {
    inventory.onOpen(true);
    inventory.onConnection({ phase: "live", since: 0 });
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "session", value: observed } }));
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "caughtUp", value: {} } }));
    watches.get("/upstream/app/v1/threads:watch")!.onFrame({ changed: {}, updatesConnected: true });
  });
  const selected = [...watches.entries()].find(([key]) => key.includes("watchSelected"))![1];
  await act(async () =>
    selected.onFrame(
      toJson(
        WatchSelectedSessionSummariesResponseSchema,
        create(WatchSelectedSessionSummariesResponseSchema, {
          observation: { case: "snapshot", value: { summaries: [value.summary] } },
        })
      )
    )
  );
  const updated = { ...value.summary, foldedThrough: 123n };
  await act(async () =>
    selected.onFrame(
      toJson(
        WatchSelectedSessionSummariesResponseSchema,
        create(WatchSelectedSessionSummariesResponseSchema, { observation: { case: "summary", value: updated } })
      )
    )
  );
  expect(container.querySelector("span")!.dataset.cursor).toBe("123");
  expect(listThreadMetadata).toHaveBeenCalledTimes(1);
});

it("marks Dispatcher activity stale when the inventory watch reconnects", async () => {
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
    const live = useThreadsLive(listing, null);
    return <span data-current={String(live.snapshot?.activity_current)} />;
  }
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root!.render(<Probe />));
  await act(async () => {
    inventory.onOpen(true);
    inventory.onConnection({ phase: "live", since: 0 });
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "session", value: observed } }));
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "caughtUp", value: {} } }));
    watches.get("/upstream/app/v1/threads:watch")!.onFrame({ changed: {}, updatesConnected: true });
  });
  const selected = [...watches.entries()].find(([key]) => key.includes("watchSelected"))![1];
  await act(async () =>
    selected.onFrame(
      toJson(
        WatchSelectedSessionSummariesResponseSchema,
        create(WatchSelectedSessionSummariesResponseSchema, {
          observation: { case: "snapshot", value: { summaries: [value.summary] } },
        })
      )
    )
  );
  expect(container.querySelector("span")!.dataset.current).toBe("true");
  await act(async () =>
    inventory.onConnection({ phase: "reconnecting", since: 0, attempt: 1, lastError: "disconnected" })
  );
  expect(container.querySelector("span")!.dataset.current).toBe("false");
});
