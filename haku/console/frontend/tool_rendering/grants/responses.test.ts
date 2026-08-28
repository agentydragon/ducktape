import { describe, expect, it } from "vitest";

import { renderResultPreview } from "../result_entry";
import { grantsResultPreviews } from "./responses";

const ENVELOPE = {
  grant_id: "20000000-0000-4000-8000-000000000002",
  owner_agent_id: "10000000-0000-4000-8000-000000000001",
  principal: { kind: "agent" as const, agent_id: "10000000-0000-4000-8000-000000000001" },
  source_tool_call_id: "tc_create_grant",
  status: "released" as const,
  created_at: "2026-08-23T10:00:00Z",
  expires_at: "2026-08-23T11:00:00Z",
  released_at: "2026-08-23T10:15:00Z",
  revoked_at: null,
  end_reason: "probe complete",
};

const KUBERNETES_VIEW = {
  domain: "kubernetes" as const,
  grant: {
    ...ENVELOPE,
    scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox"] },
    rules: [{ api_groups: [""], resources: ["pods"], verbs: ["get", "list"] }],
  },
};

const HTTP_VIEW = {
  domain: "http" as const,
  grant: {
    ...ENVELOPE,
    spec: {
      origin: { scheme: "https" as const, host: "api.github.com", port: 443 },
      coverage: { methods: ["GET"], path_regex: null },
      credential_handle: null,
    },
  },
};

describe("grantsResultPreviews", () => {
  it("renders kubernetes and http domain results in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderResultPreview(grantsResultPreviews.create_grant, [KUBERNETES_VIEW, HTTP_VIEW], variant)
      ).not.toBeNull();
      expect(renderResultPreview(grantsResultPreviews.release_grants, [KUBERNETES_VIEW], variant)).not.toBeNull();
    }
  });
});
