// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { create, fromJson } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";

import {
  interruptTurn,
  changeModel,
  changeReasoningEffort,
  submitInput,
  CommandSubmissionRefused,
  threadObservationEntry,
  threadEvidence,
  threadNativeFrames,
  threadObservations,
  listThreadMetadata,
  verifyRetainedMembers,
  selectThreadWindow,
} from "./client";
import { SessionSchema, DeliveryState } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import {
  FeedStatus,
  EntityKeySchema,
  ReadEntityEvidenceRequestSchema,
  ReadNativeFramesRequestSchema,
  ReadSessionEventsRequestSchema,
  ReadSessionObservationsRequestSchema,
  ThreadEntityKind,
  SessionSummarySchema,
} from "../../../history_service/api/history/history_service_pb";
import { Harness, HarnessState, LaunchOutcome, SetupState } from "../../../proto/session_pb";
import { ThreadMetadataSchema } from "../../app_service_pb";

function watchedSession(id = "thread-id", name = "sandbox", uid = "sandbox-uid") {
  return create(SessionSchema, {
    sessionId: id,
    createdAt: timestampFromDate(new Date("2026-01-01T00:00:00Z")),
    placement: { kind: { case: "environmentHosted", value: { environment: { name, uid }, harness: Harness.CLAUDE } } },
  });
}

const listing = { limit: 20, search: null, includeArchived: false };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("keeps app metadata and History SessionSummary in their generated message shapes", () => {
  const metadata = create(ThreadMetadataSchema, {
    threadId: "thread-id",
    createdAt: timestampFromDate(new Date("2026-01-01T00:00:00Z")),
    name: "Generated pair",
    archived: false,
  });
  const summary = create(SessionSummarySchema, {
    sessionId: "thread-id",
    sandbox: { name: "sandbox", uid: "sandbox-uid" },
    attached: {
      sessionId: "thread-id",
      spec: {
        placement: {
          kind: {
            case: "environmentHosted",
            value: { environment: { name: "sandbox", uid: "sandbox-uid" }, harness: Harness.CLAUDE },
          },
        },
        model: "model",
      },
      harnessState: HarnessState.RUNNING,
      setupState: SetupState.NOT_REQUIRED,
      launchOutcome: LaunchOutcome.STARTED,
    },
  });

  const result = selectThreadWindow(
    { ...listing, limit: 1 },
    null,
    [watchedSession()],
    new Map([["thread-id", metadata]]),
    new Map([["thread-id", summary]])
  );

  expect(result.threads).toEqual([{ metadata, summary, session: watchedSession() }]);
});

it("filters Sandbox membership from Dispatcher Sessions", () => {
  const sessions = [
    watchedSession("thread-id", "sandbox", "opaque-uid"),
    watchedSession("other-thread-id", "other-sandbox", "other-uid"),
  ];
  const result = selectThreadWindow({ ...listing, sandboxUID: "opaque-uid" }, null, sessions, new Map(), new Map());
  expect(result.threads.map((thread) => thread.metadata.threadId)).toEqual(["thread-id"]);
});

it("reads App metadata without a Sandbox filter", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push(new Request(input, init));
    return Promise.resolve(
      Response.json({
        threads: [{ threadId: "thread-id", createdAt: "2026-01-01T00:00:00Z", name: "Generated name" }],
        archivedCount: 0,
      })
    );
  });
  const metadata = await listThreadMetadata();
  expect(metadata.get("thread-id")?.name).toBe("Generated name");
  expect(requests).toHaveLength(1);
  const url = new URL(requests[0]!.url);
  expect(url.pathname).toBe("/upstream/app/v1/threads");
  expect(url.searchParams.get("sandbox_uid")).toBeNull();
  expect(url.searchParams.get("limit")).toBe("200");
});

it.each([
  { label: "a retained harness", historicalHarness: Harness.CLAUDE },
  { label: "no retained harness provenance", historicalHarness: Harness.UNSPECIFIED },
])("uses $label when archived History has no live placement", ({ historicalHarness }) => {
  const metadata = create(ThreadMetadataSchema, {
    threadId: "archived-session",
    createdAt: timestampFromDate(new Date("2026-11-04T00:00:00Z")),
    archived: true,
  });
  const summary = create(SessionSummarySchema, {
    sessionId: "archived-session",
    feedStatus: FeedStatus.ENDED,
    sandbox: { name: "archived-sandbox", uid: "archived-sandbox-uid" },
    attached: {
      spec: { model: "archived-model" },
      historicalHarness,
      harnessState: HarnessState.STOPPED,
      setupState: SetupState.NOT_REQUIRED,
      launchOutcome: LaunchOutcome.STARTED,
    },
  });

  const result = selectThreadWindow(
    { ...listing, includeArchived: true },
    null,
    [watchedSession("archived-session", "archived-sandbox", "archived-sandbox-uid")],
    new Map([["archived-session", metadata]]),
    new Map([["archived-session", summary]])
  );
  expect(result.threads).toHaveLength(1);
  expect(result.threads[0]?.summary.attached?.historicalHarness).toBe(historicalHarness);
});

it("lists a Dispatcher Session without History or App metadata and excludes History-only metadata", () => {
  const session = watchedSession("test-unrecorded-session");
  const historyOnly = create(ThreadMetadataSchema, {
    threadId: "test-history-only",
    createdAt: timestampFromDate(new Date("2026-01-01T00:00:00Z")),
    name: "History-only thread",
  });
  const result = selectThreadWindow(listing, null, [session], new Map([["test-history-only", historyOnly]]), new Map());
  expect(result.threads.map((thread) => thread.metadata.threadId)).toEqual([session.sessionId]);
  expect(result.threads[0]?.session).toEqual(session);
  expect(result.threads[0]?.summary.attached).toBeUndefined();
});

it.each(["before", "after"] as const)("preserves %s archive cursors above JS integer precision", async (direction) => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    requests.push(request);
    const url = new URL(request.url);
    const bound = BigInt(url.searchParams.get(`${direction}_cursor`) ?? "0");
    const cursors =
      direction === "before"
        ? Array.from({ length: 30 }, (_, index) => (bound - 30n + BigInt(index)).toString())
        : [(bound + 1n).toString()];
    return Promise.resolve(
      Response.json({
        lastCursor: (direction === "before" ? bound - 1n : bound + 1n).toString(),
        observations: cursors.map((cursor) => ({ cursor, kind: "EVENT_KIND_NATIVE" })),
        settled: [],
      })
    );
  });
  await threadObservations(
    create(ReadSessionObservationsRequestSchema, {
      sessionId: "test-thread",
      beforeCursor: direction === "before" ? 9007199254740993n : undefined,
      afterCursor: direction === "after" ? 9007199254740993n : undefined,
      limit: 30,
    })
  );
  expect(requests).toHaveLength(1);
  const url = new URL(requests[0]!.url);
  expect(url.pathname).toBe("/upstream/history/v1/sessions/test-thread/observations");
  expect(url.searchParams.get(`${direction}_cursor`)).toBe("9007199254740993");
  expect(url.searchParams.get("limit")).toBe("30");
});

it.each([
  {
    label: "latest",
    request: {},
    observations: [
      { cursor: "31", kind: "EVENT_KIND_HARNESS_STDERR" },
      { cursor: "34", kind: "EVENT_KIND_ITEM_COMPLETED" },
    ],
    settled: [
      { first: "1", last: "30" },
      { first: "32", last: "33" },
    ],
    cursors: ["31", "34"],
  },
  {
    label: "older",
    request: { before: "32" },
    observations: [{ cursor: "31", kind: "EVENT_KIND_HARNESS_STDERR" }],
    settled: [{ first: "1", last: "30" }],
    cursors: ["31"],
  },
  {
    label: "newer",
    request: { after: "31" },
    observations: [{ cursor: "34", kind: "EVENT_KIND_ITEM_COMPLETED" }],
    settled: [{ first: "32", last: "33" }],
    cursors: ["34"],
  },
])(
  "accepts short $label History pages when settlements cover missing cursors",
  async ({ request, observations, settled, cursors }) => {
    vi.stubGlobal("fetch", () => Promise.resolve(Response.json({ lastCursor: "34", observations, settled })));
    const bounds = request as { before?: string; after?: string };
    await expect(
      threadObservations(
        create(ReadSessionObservationsRequestSchema, {
          sessionId: "test-thread",
          beforeCursor: bounds.before === undefined ? undefined : BigInt(bounds.before),
          afterCursor: bounds.after === undefined ? undefined : BigInt(bounds.after),
          limit: 30,
        })
      )
    ).resolves.toMatchObject({
      lastCursor: 34n,
      observations: cursors.map((cursor) => ({ cursor: BigInt(cursor) })),
      settled: settled.map((range) => ({ first: BigInt(range.first), last: BigInt(range.last) })),
    });
  }
);

it.each([
  { label: "an uncovered gap", cursor: {}, lastCursor: "3", observations: [{ cursor: "1" }, { cursor: "3" }] },
  { label: "an entry past the watermark", cursor: {}, lastCursor: "0", observations: [{ cursor: "1" }] },
  { label: "missing entries", cursor: {}, lastCursor: "3", observations: [] },
  { label: "a cursor past the watermark", cursor: { after: "1" }, lastCursor: "0", observations: [] },
  {
    label: "entries before the requested cursor",
    cursor: { after: "2" },
    lastCursor: "7",
    observations: [{ cursor: "1" }, { cursor: "2" }],
  },
])("rejects an observation page with $label", async ({ cursor, lastCursor, observations }) => {
  vi.stubGlobal("fetch", () => Promise.resolve(Response.json({ lastCursor, observations, settled: [] })));
  const bounds = cursor as { before?: string; after?: string };
  await expect(
    threadObservations(
      create(ReadSessionObservationsRequestSchema, {
        sessionId: "test-thread",
        beforeCursor: bounds.before === undefined ? undefined : BigInt(bounds.before),
        afterCursor: bounds.after === undefined ? undefined : BigInt(bounds.after),
        limit: 30,
      })
    )
  ).rejects.toThrow();
});

it("reads one History event with generated requests and keeps the 64-bit cursor exact", async () => {
  const requests: Request[] = [];
  const cursor = "9007199254740993";
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    requests.push(request);
    const path = new URL(request.url).pathname;
    if (path.endsWith("/summary")) {
      return Promise.resolve(Response.json({ sessionId: "test-thread", foldedThrough: cursor }));
    }
    if (path.endsWith("/observations")) {
      return Promise.resolve(Response.json({ lastCursor: cursor, observations: [], settled: [] }));
    }
    return Promise.resolve(
      Response.json({
        lastCursor: cursor,
        entries: [{ cursor, event: {} }],
        settlements: [],
        removed: [],
      })
    );
  });

  await expect(
    threadObservationEntry(
      create(ReadSessionEventsRequestSchema, {
        sessionId: "test-thread",
        afterCursor: BigInt(cursor) - 1n,
        limit: 1,
      })
    )
  ).resolves.toMatchObject({
    lastCursor: BigInt(cursor),
    entries: [{ cursor: BigInt(cursor) }],
  });
  expect(requests.map((request) => new URL(request.url).pathname)).toEqual([
    "/upstream/history/v1/sessions/test-thread/summary",
    "/upstream/history/v1/sessions/test-thread/observations",
    "/upstream/history/v1/sessions/test-thread/events",
  ]);
  const eventUrl = new URL(requests[2]!.url);
  expect(eventUrl.searchParams.get("after_cursor")).toBe("9007199254740992");
  expect(eventUrl.searchParams.get("limit")).toBe("1");
});

it("treats a settled History cursor as an absent observation", async () => {
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path.endsWith("/summary")) return Promise.resolve(Response.json({ foldedThrough: 20 }));
    if (path.endsWith("/observations")) {
      return Promise.resolve(Response.json({ lastCursor: 20, observations: [], settled: [] }));
    }
    return Promise.resolve(
      Response.json({
        lastCursor: 20,
        entries: [{ cursor: "11", event: {} }],
        settlements: [{ ranges: [{ first: "10", last: "10" }] }],
        removed: [],
      })
    );
  });

  await expect(
    threadObservationEntry(
      create(ReadSessionEventsRequestSchema, { sessionId: "test-thread", afterCursor: 9n, limit: 1 })
    )
  ).rejects.toMatchObject({ status: 404 });
});

it("passes History protobuf evidence and native frame pages through unchanged", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    requests.push(request);
    return Promise.resolve(
      request.url.includes("entity-evidence")
        ? Response.json({
            observations: [{ observationCursor: "9007199254740993", hasNative: true }],
            nextAfterCursor: "9007199254740993",
          })
        : Response.json({
            frames: [{ sourceSequence: "9007199254740994", entry: { cursor: "9007199254740994" } }],
            nextAfterSequence: "9007199254740994",
          })
    );
  });

  await expect(
    threadEvidence(
      create(ReadEntityEvidenceRequestSchema, {
        sessionId: "session-id",
        projectionEpoch: "history-v1",
        entity: create(EntityKeySchema, { entityKind: ThreadEntityKind.ITEM, entityId: "item-id" }),
        afterCursor: 9007199254740992n,
        limit: 30,
      })
    )
  ).resolves.toMatchObject({
    observations: [{ observationCursor: 9007199254740993n, hasNative: true }],
    nextAfterCursor: 9007199254740993n,
  });
  await expect(
    threadNativeFrames(
      create(ReadNativeFramesRequestSchema, {
        sessionId: "session-id",
        projectionEpoch: "history-v1",
        entity: create(EntityKeySchema, { entityKind: ThreadEntityKind.ITEM, entityId: "item-id" }),
        observationCursor: 9007199254740993n,
        afterSequence: 9007199254740992n,
        limit: 30,
      })
    )
  ).resolves.toMatchObject({
    frames: [{ sourceSequence: 9007199254740994n, entry: { cursor: 9007199254740994n } }],
    nextAfterSequence: 9007199254740994n,
  });

  for (const [request, route] of [
    [requests[0]!, "/upstream/history/v1/sessions/session-id/entity-evidence"],
    [requests[1]!, "/upstream/history/v1/sessions/session-id/native-frames"],
  ] as const) {
    expect(request.method).toBe("POST");
    expect(new URL(request.url).pathname).toBe(route);
    const body = await request.clone().json();
    expect(body).not.toHaveProperty("sessionId");
    expect(body).toMatchObject({
      projectionEpoch: "history-v1",
      limit: 30,
    });
    const entity = fromJson(EntityKeySchema, body.entity);
    expect(entity.entityKind).toBe(ThreadEntityKind.ITEM);
    expect(entity.entityId).toBe("item-id");
  }
  expect(await requests[0]!.clone().json()).toMatchObject({ afterCursor: "9007199254740992" });
  expect(await requests[1]!.clone().json()).toMatchObject({
    observationCursor: "9007199254740993",
    afterSequence: "9007199254740992",
  });
});
const input = { commandId: "immutable-command", text: "keep this input" };
const target = { sandbox: "original-sandbox", sandboxUid: "original-uid" };

it.each([
  [504, 4],
  [502, 14],
  [408, undefined],
  [404, 5],
  [400, 9],
  [429, 8],
])("keeps HTTP %i / gRPC %s without a receipt unconfirmed", async (status, code) => {
  vi.stubGlobal("fetch", () => Response.json({ code, message: "not confirmed" }, { status }));
  const failure: unknown = await submitInput("thread", input.commandId, input.text).catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(Error);
  expect(failure).not.toBeInstanceOf(CommandSubmissionRefused);
});

it.each([
  [400, 3],
  [409, 6],
  [403, 7],
  [403, undefined],
])("recognizes HTTP %i / gRPC %s invariant or authorization rejection", async (status, code) => {
  vi.stubGlobal("fetch", () => Response.json({ code, message: "refused" }, { status }));
  await expect(submitInput("thread", input.commandId, input.text)).rejects.toBeInstanceOf(CommandSubmissionRefused);
});

it.each([
  {
    operation: "submitInput",
    fields: { text: input.text },
    call: () => submitInput("thread", input.commandId, input.text),
  },
  {
    operation: "changeModel",
    fields: { model: "vendor/model" },
    call: () => changeModel("thread", input.commandId, "vendor/model"),
  },
  {
    operation: "changeReasoningEffort",
    fields: { effort: "high" },
    call: () => changeReasoningEffort("thread", input.commandId, "high"),
  },
])("uses the generated $operation route and preserves a queued Dispatch", async ({ operation, fields, call }) => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    requests.push(new Request(url, init));
    return Response.json({
      dispatch: {
        commandId: input.commandId,
        sessionId: "thread",
        ...target,
        state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
        reason: "sandbox_resuming",
        updatedAt: "2026-10-14T12:00:00Z",
      },
    });
  });
  const result = await call();
  expect(result.kind).toBe("dispatch");
  if (result.kind !== "dispatch") throw new Error("Expected Dispatcher receipt");
  expect(result.dispatch).toMatchObject({
    commandId: input.commandId,
    sessionId: "thread",
    ...target,
    state: DeliveryState.QUEUED_IN_DISPATCHER,
    reason: "sandbox_resuming",
    updatedAt: { seconds: BigInt(Date.parse("2026-10-14T12:00:00Z") / 1000), nanos: 0 },
  });
  expect(result.dispatch.admission).toBeUndefined();
  expect(requests).toHaveLength(1);
  expect(new URL(requests[0]!.url).pathname).toBe(`/upstream/dispatcher/v1/sessions/thread:${operation}`);
  expect(requests[0]!.method).toBe("POST");
  expect(await requests[0]!.json()).toEqual({
    commandId: input.commandId,
    ...fields,
  });
});

it("returns a durable Dispatch for an interrupt", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    requests.push(new Request(url, init));
    return Response.json({
      dispatch: {
        commandId: "interrupt-id",
        sessionId: "thread",
        ...target,
        state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
      },
    });
  });
  const result = await interruptTurn("thread", "interrupt-id", "turn-id");
  expect(result).toMatchObject({
    kind: "dispatch",
    dispatch: { state: DeliveryState.QUEUED_IN_DISPATCHER },
  });
  expect(new URL(requests[0]!.url).pathname).toBe("/upstream/dispatcher/v1/sessions/thread:interruptTurn");
  expect(await requests[0]!.json()).toEqual({
    commandId: "interrupt-id",
    turnId: "turn-id",
  });
});

it.each([
  {},
  { dispatch: { commandId: input.commandId, sessionId: "thread", ...target } },
  {
    dispatch: {
      commandId: input.commandId,
      sessionId: "thread",
      ...target,
      sandboxUid: "",
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    },
  },
  {
    dispatch: {
      commandId: "other-command",
      sessionId: "thread",
      ...target,
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    },
  },
  {
    dispatch: {
      commandId: input.commandId,
      sessionId: "other-session",
      ...target,
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    },
  },
])("does not acknowledge a missing or mismatched generated receipt: %j", async (body) => {
  vi.stubGlobal("fetch", () => Response.json(body));
  const failure: unknown = await submitInput("thread", input.commandId, input.text).catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(Error);
  expect(failure).not.toBeInstanceOf(CommandSubmissionRefused);
});

it("retries the same Session command and payload after a lost transport reply", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    requests.push(new Request(url, init));
    throw new TypeError("connection lost");
  });
  for (let attempt = 0; attempt < 2; attempt++) {
    const failure: unknown = await submitInput("thread", input.commandId, input.text).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(CommandSubmissionRefused);
  }
  expect(requests).toHaveLength(2);
  for (const request of requests) {
    expect(new URL(request.url).pathname).toBe("/upstream/dispatcher/v1/sessions/thread:submitInput");
    expect(await request.json()).toEqual(input);
  }
});

it.each([
  [{}, false],
  [{ retainsCommandsAfterEnvironmentDeletion: false }, false],
  [{ retainsCommandsAfterEnvironmentDeletion: true }, true],
  [{ retainsCommandsAfterEnvironmentDeletion: true, nextPageToken: "another" }, false],
])("requires positive database retention and complete named membership %j", async (fields, accepted) => {
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input.toString(), location.href);
    expect(url.pathname).toBe("/upstream/dispatcher/v1/sessions/thread/dispatches");
    expect([...url.searchParams]).toEqual([
      ["verifyRetainedCommandIds", "a"],
      ["verifyRetainedCommandIds", "b"],
    ]);
    expect(init?.method ?? "GET").toBe("GET");
    return Response.json({
      coverage: "RETAINED_DISPATCH_ROWS",
      dispatches: [{ sessionId: "thread", commandId: "b", sandbox: "box", sandboxUid: "uid", receiptVersion: "3" }],
      ...fields,
    });
  });
  const result = await verifyRetainedMembers("thread", ["a", "b"], new AbortController().signal);
  expect(result.map((row) => row.commandId)).toEqual(accepted ? ["b"] : []);
});
