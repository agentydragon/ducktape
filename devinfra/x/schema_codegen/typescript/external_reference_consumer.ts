import type { paths } from "./api/external_openapi_types";
import type { ExternalResult } from "./external_json_schema_types";
import { zExternalResult } from "./external_openapi_zod";

type OpenApiResult = paths["/fixture"]["get"]["responses"][200]["content"]["application/json"];

export const openApiResult: OpenApiResult = {
  id: 17,
  label: "from another package",
  created_at: "2026-10-27T12:00:00Z",
};
export const jsonSchemaResult: ExternalResult = {
  id: 23,
  label: "declared reference input",
  created_at: "2026-10-27T12:00:00Z",
};

export function validatesOffsetTimestamp(input: unknown): boolean {
  return zExternalResult.safeParse(input).success;
}
