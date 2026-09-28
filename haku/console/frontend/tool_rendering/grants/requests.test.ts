import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions";
import { renderPreview } from "../entry";
import { GRANTS_SERVER_ID } from "../server_ids";
import { grantsPreviews } from "./requests";

const KUBERNETES_ITEM = {
  scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox"] },
  rules: [{ api_groups: [""], resources: ["pods"], verbs: ["get", "list"] }],
};

const AGENT_PRINCIPAL = { kind: "agent" as const, agent_id: "10000000-0000-4000-8000-000000000001" };

describe("grantsPreviews", () => {
  it("renders grant creation in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderPreview(
          grantsPreviews.create_grant,
          { grants: [KUBERNETES_ITEM], duration_seconds: 3600, principal: AGENT_PRINCIPAL },
          variant
        )
      ).not.toBeNull();
    }
    expect(
      toolActionDescription(GRANTS_SERVER_ID, "create_grant", {
        grants: [KUBERNETES_ITEM],
        duration_seconds: 3600,
        principal: AGENT_PRINCIPAL,
      })?.text
    ).toBe("Grants: Create Kubernetes 1 grant");
  });

  it("renders the self principal shorthand", () => {
    expect(
      renderPreview(
        grantsPreviews.create_grant,
        { grants: [KUBERNETES_ITEM], duration_seconds: 3600, principal: "self" },
        "detailed"
      )
    ).not.toBeNull();
  });

  it("names an end without distinguishing the caller", () => {
    const args = {
      grant_ids: ["20000000-0000-4000-8000-000000000002", "20000000-0000-4000-8000-000000000003"],
      reason: "probe complete",
    };
    expect(renderPreview(grantsPreviews.revoke_grants, args, "compact")).not.toBeNull();
    expect(toolActionDescription(GRANTS_SERVER_ID, "revoke_grants", args)).toEqual({
      text: "Grants: End 2 grants",
      destructive: true,
    });
  });

  it("uses the same end label when an Operator supplies owner_agent_id", () => {
    const args = {
      owner_agent_id: "10000000-0000-4000-8000-000000000001",
      grant_ids: ["20000000-0000-4000-8000-000000000002"],
      reason: "operator revoked",
    };
    expect(renderPreview(grantsPreviews.revoke_grants, args, "detailed")).not.toBeNull();
    expect(toolActionDescription(GRANTS_SERVER_ID, "revoke_grants", args)).toEqual({
      text: "Grants: End 1 grant",
      destructive: true,
    });
  });

  it("falls back when a grant call does not match its advertised schema", () => {
    expect(renderPreview(grantsPreviews.create_grant, { grants: [] }, "detailed")).toBeNull();
  });
});
