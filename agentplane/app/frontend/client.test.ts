// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

import { api, fetchWithLogin, HttpError, ok } from "./client";
import { restoreRouteAfterLogin } from "./operator_login";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("throws a non-2xx answer as an HttpError with its status and the complete structured body", async () => {
  const body = {
    detail: { method: "GET", url: "https://upstream.invalid/mcp", upstream_status: null, error_type: "ConnectError" },
  };
  const middleware = { onRequest: () => Response.json(body, { status: 503 }) };
  api.use(middleware);
  try {
    const failure: unknown = await api
      .GET("/models")
      .then((answer) => ok(answer))
      .catch((error: unknown) => error);
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
