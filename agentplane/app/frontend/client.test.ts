// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

import {
  api,
  fetchWithLogin,
  interruptTurn,
  submitInput,
  CommandSubmissionRefused,
  HttpError,
  threadObservations,
} from "./client";
import { restoreRouteAfterLogin } from "./operator_login";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it.each(["before", "after"] as const)("preserves %s archive cursors above JS integer precision", async (direction) => {
  const requests: URL[] = [];
  const middleware = {
    onRequest({ request }: { request: Request }) {
      requests.push(new URL(request.url, "https://app.invalid"));
      return Response.json({ observations: [], next_before_cursor: null, next_after_cursor: null });
    },
  };
  api.use(middleware);
  try {
    await threadObservations("test-thread", { [direction]: "9007199254740993" });
    expect(requests).toHaveLength(1);
    expect(requests[0].searchParams.get(`${direction}_cursor`)).toBe("9007199254740993");
    expect(requests[0].searchParams.get("limit")).toBe("30");
  } finally {
    api.eject(middleware);
  }
});

it("throws a non-2xx answer as an HttpError with its status and the complete structured body", async () => {
  const body = {
    detail: { method: "GET", url: "https://upstream.invalid/mcp", upstream_status: null, error_type: "ConnectError" },
  };
  const middleware = { onRequest: () => Response.json(body, { status: 503 }) };
  api.use(middleware);
  try {
    const failure: unknown = await threadObservations("test-thread", {}).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(HttpError);
    expect(failure).toMatchObject({ status: 503, body });
  } finally {
    api.eject(middleware);
  }
});

it("on a 401 sends the browser to log in once and never hands the response back to the page", async () => {
  const replace = vi.fn();
  vi.stubGlobal("location", { pathname: "/", hash: "#/mcp-servers", replace });
  const answer = (status: number) => async () =>
    Response.json({ detail: "no session and no accepted token" }, { status });
  vi.stubGlobal("fetch", answer(401));
  const unauthorized = [
    fetchWithLogin("/upstream/actions/v1/action-groups"),
    fetchWithLogin("/upstream/actions/v1/action-requests:watch"),
  ];
  // The same pipeline one request later, minus the redirect: had either 401 been handed back to
  // the page, it would have settled before this does.
  const control = api.GET("/models", { fetch: answer(403) });
  await expect(
    Promise.race([
      ...unauthorized.map((request) => request.then(() => "401 reached the page")),
      control.then(() => "control settled"),
    ])
  ).resolves.toBe("control settled");
  expect(replace.mock.calls).toEqual([["/auth/login"]]);
  const afterLogin = { hash: "" };
  restoreRouteAfterLogin(afterLogin);
  expect(afterLogin.hash).toBe("#/mcp-servers");
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
    await expect(submitInput("thread", input.commandId, input.text)).resolves.toEqual({
      kind: "dispatch",
      dispatch: {
        commandId: input.commandId,
        sessionId: "thread",
        state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
        reason: "sandbox_resuming",
        updatedAt: "2026-10-14T12:00:00Z",
        admission: null,
      },
    });
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
    await expect(interruptTurn("thread", "interrupt-id", "turn-id")).resolves.toEqual({
      kind: "interrupt_result",
      result: { state: "DELIVERY_STATE_NO_OP", reason: "turn already ended", admission: null },
    });
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
