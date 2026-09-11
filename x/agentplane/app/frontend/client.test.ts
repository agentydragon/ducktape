import { expect, it } from "vitest";

import { displayableError, httpError } from "./client";

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
