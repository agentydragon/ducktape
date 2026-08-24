import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry";
import { kubernetesResultPreviews } from "./responses";

const GRANT = {
  grant_id: "20000000-0000-4000-8000-000000000002",
  agent_id: "10000000-0000-4000-8000-000000000001",
  source_tool_call_id: "tc_create_grant",
  scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox"] },
  rules: [{ api_groups: [""], resources: ["pods"], verbs: ["get", "list"] }],
  status: "released" as const,
  created_at: "2026-08-23T10:00:00Z",
  expires_at: "2026-08-23T11:00:00Z",
  ended_at: "2026-08-23T10:15:00Z",
  end_reason: "probe complete",
};

describe("kubernetesResultPreviews", () => {
  it("renders create and release results in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(renderResultPreview(kubernetesResultPreviews.create_grant, [GRANT], variant)).not.toBeNull();
      expect(renderResultPreview(kubernetesResultPreviews.release_grants, [GRANT], variant)).not.toBeNull();
    }
  });
});
