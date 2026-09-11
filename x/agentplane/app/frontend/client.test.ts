import { expect, it } from "vitest";

import { displayableError } from "./client";

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
