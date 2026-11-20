// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import {
  SessionSchema,
  WatchSessionsResponseSchema,
} from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { sessionDispatcherClient } from "../transport/session_dispatcher_client";
import { followWatch, type WatchHandlers } from "../transport/watch_stream";
import { listThreadWindow, type ThreadWindow } from "./client";
import { threadView } from "./testing/state";
import { combinedConnection, useThreadsLive } from "./live";
import { threadStatusFromSnapshot } from "./thread_status";
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
  listThreadWindow: vi.fn(),
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

it("publishes activity during a delayed History join and invalidates it at watch loss", async () => {
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
  const observed = create(SessionSchema, {
    sessionId: id,
    placement: value.summary.attached!.spec!.placement,
    status: {
      activity: {
        kind: {
          case: "observed",
          value: {
            sessionId: id,
            activeTurnId: "active-turn",
            host: {
              case: "environmentHosted",
              value: {
                setup: 1,
                harness: { state: 3 },
              },
            },
          },
        },
      },
    },
  });
  vi.mocked(listThreadWindow).mockResolvedValue({
    threads: [{ ...value, session: observed }],
    more: false,
    archived_count: 0,
  });
  const listing = { limit: 50, search: null, includeArchived: false };
  function Probe(): JSX.Element {
    const live = useThreadsLive(listing, null);
    const status = threadStatusFromSnapshot(live.snapshot?.threads[0], live.snapshot?.activity_current === true);
    return <span data-status={status.kind} />;
  }
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root!.render(<Probe />));
  const frame = (session: typeof observed): void =>
    inventory.onFrame(
      create(WatchSessionsResponseSchema, {
        frame: { case: "session", value: session },
        resumeToken: "checkpoint",
      })
    );
  await act(async () => {
    inventory.onOpen(true);
    inventory.onConnection({ phase: "live", since: 0 });
    frame(observed);
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "caughtUp", value: {} } }));
  });
  expect(container.querySelector("span")!.dataset.status).toBe("running");

  // An already-started join holds the old observation while a new upsert arrives.
  let completeJoin!: (value: ThreadWindow) => void;
  const delayedJoin = new Promise<ThreadWindow>((resolve) => {
    completeJoin = resolve;
  });
  vi.mocked(listThreadWindow).mockReturnValueOnce(delayedJoin);
  await act(async () => {
    watches.get("/upstream/history/v1/session-summaries:watch")!.onFrame({ changed: {} });
    watches.get("/upstream/app/v1/threads:watch")!.onFrame({ changed: {}, updatesConnected: true });
  });
  const unknownSession = create(SessionSchema, {
    sessionId: id,
    placement: observed.placement,
    status: { activity: { kind: { case: "unknown", value: { reason: 2 } } } },
  });
  await act(async () => frame(unknownSession));
  expect(container.querySelector("span")!.dataset.status).toBe("unknown");
  await act(async () => completeJoin({ threads: [{ ...value, session: observed }], more: false, archived_count: 0 }));
  expect(container.querySelector("span")!.dataset.status).toBe("unknown");

  await act(async () => frame(observed));
  expect(container.querySelector("span")!.dataset.status).toBe("running");
  await act(async () =>
    watches
      .get("/upstream/history/v1/session-summaries:watch")!
      .onConnection({ phase: "reconnecting", since: 0, attempt: 1, lastError: "History rolled" })
  );
  expect(container.querySelector("span")!.dataset.status).toBe("running");
  await act(async () =>
    inventory.onConnection({ phase: "reconnecting", since: 0, attempt: 1, lastError: "Dispatcher rolled" })
  );
  expect(container.querySelector("span")!.dataset.status).toBe("unknown");
  // A partial replacement snapshot cannot make its first upsert authoritative.
  await act(async () => {
    inventory.onOpen(true);
    inventory.onConnection({ phase: "live", since: 0 });
    frame(observed);
  });
  expect(container.querySelector("span")!.dataset.status).toBe("unknown");
  await act(async () =>
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "caughtUp", value: {} } }))
  );
  expect(container.querySelector("span")!.dataset.status).toBe("running");
  await act(async () =>
    inventory.onFrame(create(WatchSessionsResponseSchema, { frame: { case: "reconnectRequired", value: {} } }))
  );
  expect(container.querySelector("span")!.dataset.status).toBe("unknown");
});
