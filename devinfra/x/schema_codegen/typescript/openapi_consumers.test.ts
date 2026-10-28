import { expect, it } from "vitest";

import type { CreateInvoiceRequest } from "./openapi_types";
import { serializedResponse } from "./typed_response_fixture";
import {
  inputUnitPrice,
  serializedUnitPrice,
  traceIds,
  validateCreateInvoiceRequest,
  validateInvoiceResponse,
} from "./openapi_consumer";

const validRequest: CreateInvoiceRequest = {
  memo: null,
  submitted_at: "2026-10-27T09:30:00-07:00",
  payment: { kind: "card", last_four: "1234" },
  lines: [{ sku: "lamp", quantity: 2, unit_price: 19.95 }],
  named_lines: { primary: { sku: "lamp", quantity: 2, unit_price: 19.95 } },
};

it("validates FastAPI requests with nested refs, records, unions, and offset timestamps", () => {
  expect(validateCreateInvoiceRequest(validRequest)).toBe(true);
  expect(inputUnitPrice(validRequest.lines[0])).toBe(19.95);
  expect(validateCreateInvoiceRequest({ ...validRequest, memo: undefined })).toBe(true);
  expect(
    validateCreateInvoiceRequest({
      ...validRequest,
      payment: { kind: "card", last_four: "12x4" },
    })
  ).toBe(false);
  expect(validateCreateInvoiceRequest({ ...validRequest, submitted_at: "2026-10-27T09:30:00" })).toBe(false);
});

it("validates serialized Decimal output and normalized acronym root names", () => {
  expect(validateInvoiceResponse(serializedResponse)).toBe(true);
  expect(validateInvoiceResponse({ ...serializedResponse, total: 39.9 })).toBe(false);
  expect(
    validateInvoiceResponse({
      ...serializedResponse,
      lines: [{ ...serializedResponse.lines[0], unit_price: 19.95 }],
    })
  ).toBe(false);
  expect(serializedUnitPrice(serializedResponse.lines[0])).toBe("19.95");
  expect(traceIds(serializedResponse.trace, serializedResponse.http_trace)).toBe("req-http:req-title");
});
