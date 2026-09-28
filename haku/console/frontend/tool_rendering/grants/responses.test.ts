import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry";
import { grantsResultPreviews } from "./responses";

const KUBERNETES_VIEW = {
  grant_id: "20000000-0000-4000-8000-000000000002",
  owner_agent_id: "10000000-0000-4000-8000-000000000001",
  principal: { kind: "agent" as const, agent_id: "10000000-0000-4000-8000-000000000001" },
  source_tool_call_id: "tc_create_grant",
  status: "ended" as const,
  created_at: "2026-08-23T10:00:00Z",
  expires_at: "2026-08-23T11:00:00Z",
  ended_at: "2026-08-23T10:15:00Z",
  end_reason: "probe complete",
  scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox"] },
  rules: [{ api_groups: [""], resources: ["pods"], verbs: ["get", "list"] }],
};

const ACCESS_PROFILE_VIEW = {
  ...KUBERNETES_VIEW,
  principal: { kind: "access_profile" as const, access_profile_id: "public-coder" },
};

describe("grantsResultPreviews", () => {
  it("renders grant results in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(grantsResultPreviews.create_grant, [KUBERNETES_VIEW, ACCESS_PROFILE_VIEW], variant)
      ).not.toBeNull();
      expect(renderResultPreview(grantsResultPreviews.revoke_grants, [KUBERNETES_VIEW], variant)).not.toBeNull();
    }
  });
});
