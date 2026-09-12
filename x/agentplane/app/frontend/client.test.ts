// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";

import { api, displayableError, httpError } from "./client";
import { restoreRouteAfterLogin } from "./operator_login";

afterEach(() => vi.unstubAllGlobals());

it("includes HTTP status and the complete structured error without interpreting its envelope", () => {
  const error = {
    detail: { method: "GET", url: "https://upstream.invalid/mcp", upstream_status: null, error_type: "ConnectError" },
  };
  expect(httpError(new Response(null, { status: 503, statusText: "Service Unavailable" }), error)).toBe(
    `HTTP 503 Service Unavailable: ${JSON.stringify(error)}`
  );
});

it("keeps the status even when HTTP/2 supplies no status text", () => {
  expect(httpError(new Response(null, { status: 502 }), "Bad gateway")).toBe("HTTP 502: Bad gateway");
});

it.each([
  [
    { detail: { code: "operator_federation_exchange_failed" } },
    '{"detail":{"code":"operator_federation_exchange_failed"}}',
  ],
  [
    { detail: { code: "link_failed", message: "GitHub authorization is unavailable" } },
    '{"detail":{"code":"link_failed","message":"GitHub authorization is unavailable"}}',
  ],
  [{ detail: "Action Service is unavailable" }, '{"detail":"Action Service is unavailable"}'],
  [new Error("Network request failed"), "Network request failed"],
  [
    { detail: [{ loc: ["body", "scopes", 0], msg: "Input should be a valid string" }] },
    '{"detail":[{"loc":["body","scopes",0],"msg":"Input should be a valid string"}]}',
  ],
  [{ detail: { unexpected: "failure" } }, '{"detail":{"unexpected":"failure"}}'],
])("preserves API error information for display: %j", (error, expected) => {
  expect(displayableError(error)).toBe(expected);
});

it("on a 401 sends the browser to log in once and never hands the response back to the page", async () => {
  const replace = vi.fn();
  vi.stubGlobal("location", { pathname: "/", hash: "#/mcp-servers", replace });
  const answer = (status: number) => async () =>
    Response.json({ detail: "no session and no accepted token" }, { status });
  const unauthorized = [api.GET("/mcp-servers", { fetch: answer(401) }), api.GET("/actions", { fetch: answer(401) })];
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
