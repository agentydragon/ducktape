import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { zCreateInvoiceRequest } from "./openapi_zod.js";
import { zInvoiceResponse } from "./openapi_zod.js";

const validRequest = {
  memo: null,
  submitted_at: "2026-10-27T09:30:00-07:00",
  payment: { kind: "card", last_four: "1234" },
  lines: [{ sku: "lamp", quantity: 2, unit_price: 19.95 }],
  named_lines: { primary: { sku: "lamp", quantity: 2, unit_price: 19.95 } },
};

assert.equal(zCreateInvoiceRequest.safeParse(validRequest).success, true);
assert.equal(
  zCreateInvoiceRequest.safeParse({
    ...validRequest,
    payment: { kind: "card", last_four: "12x4" },
  }).success,
  false
);

const serializedResponse = JSON.parse(readFileSync(new URL("./invoice_response.json", import.meta.url), "utf8"));
assert.equal(zInvoiceResponse.safeParse(serializedResponse).success, true);
