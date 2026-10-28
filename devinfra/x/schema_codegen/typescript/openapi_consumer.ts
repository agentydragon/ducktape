import type { CreateInvoiceRequest, HttpTrace, InvoiceLineInput, InvoiceLineOutput, paths } from "./openapi_types";
import { zCreateInvoiceRequest, zInvoiceResponse } from "./openapi_zod";

export type InvoiceCreateOperation = paths["/invoices"]["post"];

export function validateCreateInvoiceRequest(input: CreateInvoiceRequest): boolean {
  return zCreateInvoiceRequest.safeParse(input).success;
}

export function validateInvoiceResponse(input: unknown): boolean {
  return zInvoiceResponse.safeParse(input).success;
}

export function traceIds(trace: HttpTrace, underscored: HttpTrace): string {
  return `${trace.request_id}:${underscored.request_id}`;
}

export function inputUnitPrice(line: InvoiceLineInput): number | string {
  return line.unit_price;
}

export function serializedUnitPrice(line: InvoiceLineOutput): string {
  return line.unit_price;
}
