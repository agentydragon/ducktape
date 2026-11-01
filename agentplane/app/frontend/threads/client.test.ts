// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

import { api } from "../client";
import {
  interruptTurn,
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

it.each([504, 502, 408])("treats HTTP %i without a command receipt as unconfirmed", async (status) => {
  const middleware = { onRequest: () => Response.json({ detail: "deadline" }, { status }) };
  api.use(middleware);
  try {
    const failure: unknown = await submitInput("thread", input.commandId, input.text).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(CommandSubmissionRefused);
  } finally {
    api.eject(middleware);
  }
});

it.each([409, 422, 429, 403])("recognizes explicit HTTP %i command rejection", async (status) => {
  const middleware = { onRequest: () => Response.json({ detail: "refused" }, { status }) };
  api.use(middleware);
  try {
    await expect(submitInput("thread", input.commandId, input.text)).rejects.toBeInstanceOf(CommandSubmissionRefused);
  } finally {
    api.eject(middleware);
  }
});

it("accepts a durable queued Dispatch separately from a runner admission receipt", async () => {
  const middleware = {
    onRequest: () =>
      Response.json({
        kind: "dispatch",
        dispatch: {
          commandId: input.commandId,
          sessionId: "thread",
          state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
          reason: "sandbox_resuming",
          updatedAt: "2026-10-14T12:00:00Z",
        },
      }),
  };
  api.use(middleware);
  try {
    const result = await submitInput("thread", input.commandId, input.text);
    expect(result.kind).toBe("dispatch");
    if (result.kind !== "dispatch") throw new Error("Expected Dispatcher receipt");
    expect(result.dispatch).toMatchObject({
      commandId: input.commandId,
      sessionId: "thread",
      state: DeliveryState.QUEUED_IN_DISPATCHER,
      reason: "sandbox_resuming",
      updatedAt: { seconds: BigInt(Date.parse("2026-10-14T12:00:00Z") / 1000), nanos: 0 },
    });
    expect(result.dispatch.admission).toBeUndefined();
  } finally {
    api.eject(middleware);
  }
});

it("keeps a transient interrupt result distinct from a durable Dispatch", async () => {
  const paths: string[] = [];
  const middleware = {
    onRequest({ request }: { request: Request }) {
      paths.push(new URL(request.url, "https://app.invalid").pathname);
      return Response.json({
        kind: "interrupt_result",
        result: { state: "DELIVERY_STATE_NO_OP", reason: "turn already ended" },
      });
    },
  };
  api.use(middleware);
  try {
    const result = await interruptTurn("thread", "interrupt-id", "turn-id");
    expect(result).toMatchObject({
      kind: "interrupt_result",
      result: { state: DeliveryState.NO_OP, reason: "turn already ended" },
    });
    if (result.kind !== "interrupt_result") throw new Error("Expected immediate interrupt result");
    expect(result.result.admission).toBeUndefined();
    expect(paths).toEqual(["/threads/thread/interrupt-turn"]);
  } finally {
    api.eject(middleware);
  }
});

it("accepts a tagged archived admission after a deleted Sandbox", async () => {
  const middleware = {
    onRequest: () =>
      Response.json({
        kind: "archived_admission",
        admission: { cursor: "1", event: { commandAdmitted: { command: { commandId: input.commandId } } } },
      }),
  };
  api.use(middleware);
  try {
    await expect(submitInput("thread", input.commandId, input.text)).resolves.toMatchObject({
      kind: "archived_admission",
      admission: {
        cursor: 1n,
        event: { observation: { case: "commandAdmitted", value: { command: { commandId: input.commandId } } } },
      },
    });
  } finally {
    api.eject(middleware);
  }
});

it("treats a lost transport reply as unconfirmed without changing the submitted command", async () => {
  const requests: Request[] = [];
  const middleware = {
    onRequest({ request }: { request: Request }) {
      requests.push(request);
      throw new TypeError("connection lost");
    },
  };
  api.use(middleware);
  try {
    const failure: unknown = await submitInput("thread", input.commandId, input.text).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(CommandSubmissionRefused);
    expect(requests).toHaveLength(1);
    expect(await requests[0].json()).toMatchObject({ commandId: input.commandId });
  } finally {
    api.eject(middleware);
  }
});
