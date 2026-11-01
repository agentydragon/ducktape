// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

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
} from "./client";
import { DeliveryState } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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
        observations: cursors.map((cursor) => ({ cursor, kind: "test" })),
        settled: [],
      })
    );
  });
  await threadObservations("test-thread", { [direction]: "9007199254740993" });
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
      { cursor: "31", kind: "harness_stderr" },
      { cursor: "34", kind: "item_completed" },
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
    observations: [{ cursor: "31", kind: "harness_stderr" }],
    settled: [{ first: "1", last: "30" }],
    cursors: ["31"],
  },
  {
    label: "newer",
    request: { after: "31" },
    observations: [{ cursor: "34", kind: "item_completed" }],
    settled: [{ first: "32", last: "33" }],
    cursors: ["34"],
  },
])(
  "accepts short $label History pages when settlements cover missing cursors",
  async ({ request, observations, settled, cursors }) => {
    vi.stubGlobal("fetch", () => Promise.resolve(Response.json({ lastCursor: "34", observations, settled })));
    await expect(threadObservations("test-thread", request)).resolves.toMatchObject({
      observations: cursors.map((cursor) => ({ cursor })),
    });
  }
);

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

  await expect(threadObservationEntry("test-thread", cursor)).resolves.toMatchObject({
    cursor,
    entry: { cursor },
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

  await expect(threadObservationEntry("test-thread", "10")).rejects.toMatchObject({ status: 404 });
});

it("uses generated History requests and adapts evidence and native frame pages", async () => {
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
      "session-id",
      { projectionEpoch: "history-v1", entityKind: "item", entityId: "item-id" },
      "9007199254740992"
    )
  ).resolves.toEqual({
    observations: [{ observation_cursor: "9007199254740993", has_native: true }],
    next_after_cursor: "9007199254740993",
  });
  await expect(
    threadNativeFrames(
      "session-id",
      { projectionEpoch: "history-v1", entityKind: "item", entityId: "item-id" },
      "9007199254740993",
      "9007199254740992"
    )
  ).resolves.toEqual({
    frames: [
      {
        source_sequence: "9007199254740994",
        availability: "present",
        entry: { cursor: "9007199254740994" },
      },
    ],
    next_after_sequence: "9007199254740994",
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
      entity: { entityKind: "item", entityId: "item-id" },
      limit: 30,
    });
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
  const failure: unknown = await submitInput("thread", target, input.commandId, input.text).catch(
    (error: unknown) => error
  );
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
  await expect(submitInput("thread", target, input.commandId, input.text)).rejects.toBeInstanceOf(
    CommandSubmissionRefused
  );
});

it.each([
  {
    operation: "submitInput",
    fields: { text: input.text },
    call: () => submitInput("thread", target, input.commandId, input.text),
  },
  {
    operation: "changeModel",
    fields: { model: "vendor/model" },
    call: () => changeModel("thread", target, input.commandId, "vendor/model"),
  },
  {
    operation: "changeReasoningEffort",
    fields: { effort: "high" },
    call: () => changeReasoningEffort("thread", target, input.commandId, "high"),
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
  expect(await requests[0]!.json()).toEqual({ commandId: input.commandId, ...target, ...fields });
});

it("keeps the generated immediate interrupt result separate from a durable Dispatch", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    requests.push(new Request(url, init));
    return Response.json({ state: "DELIVERY_STATE_NO_OP", reason: "turn already ended" });
  });
  const result = await interruptTurn("thread", target, "interrupt-id", "turn-id");
  expect(result).toMatchObject({
    kind: "interrupt_result",
    result: { state: DeliveryState.NO_OP, reason: "turn already ended" },
  });
  expect(new URL(requests[0]!.url).pathname).toBe("/upstream/dispatcher/v1/sessions/thread:interruptTurn");
  expect(await requests[0]!.json()).toEqual({ commandId: "interrupt-id", ...target, turnId: "turn-id" });
});

it.each([
  {},
  { dispatch: { commandId: input.commandId, sessionId: "thread", ...target } },
  {
    dispatch: {
      commandId: input.commandId,
      sessionId: "thread",
      ...target,
      sandboxUid: "replacement-uid",
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
  const failure: unknown = await submitInput("thread", target, input.commandId, input.text).catch(
    (error: unknown) => error
  );
  expect(failure).toBeInstanceOf(Error);
  expect(failure).not.toBeInstanceOf(CommandSubmissionRefused);
});

it("keeps the original command and Sandbox incarnation after a lost transport reply", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", (url: RequestInfo | URL, init?: RequestInit) => {
    requests.push(new Request(url, init));
    throw new TypeError("connection lost");
  });
  for (let attempt = 0; attempt < 2; attempt++) {
    const failure: unknown = await submitInput("thread", target, input.commandId, input.text).catch(
      (error: unknown) => error
    );
    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(CommandSubmissionRefused);
  }
  expect(requests).toHaveLength(2);
  for (const request of requests) expect(await request.json()).toEqual({ ...input, ...target });
});
