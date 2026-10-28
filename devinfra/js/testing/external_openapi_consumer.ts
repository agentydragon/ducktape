import type { paths } from "./external_openapi_types";

export type FixtureResponse = paths["/fixture"]["get"]["responses"][200]["content"]["application/json"];

export const fixtureResponse: FixtureResponse = {
  id: 17,
  label: "from a cross-package local ref",
  created_at: "2026-10-27T12:00:00Z",
};
