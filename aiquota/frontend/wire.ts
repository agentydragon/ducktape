import { zAllQuotasView } from "./api/schema.zod";
import type { QuotasView } from "./quotas";

export function parseQuotasView(text: string): QuotasView {
  const value: unknown = JSON.parse(text);
  return zAllQuotasView.parse(value);
}
