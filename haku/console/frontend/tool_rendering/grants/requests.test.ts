import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions";
import { renderPreview } from "../entry";
import { GRANTS_SERVER_ID } from "../server_ids";
import { grantsPreviews } from "./requests";

const KUBERNETES_ITEM = {
  domain: "kubernetes" as const,
  spec: {
    scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox"] },
    rules: [{ api_groups: [""], resources: ["pods"], verbs: ["get", "list"] }],
  },
};

const HTTP_ITEM = {
  domain: "http" as const,
  spec: {
    origin: { scheme: "https" as const, host: "api.github.com", port: 443 },
    coverage: { methods: ["GET"], path_regex: "/repos/.*" },
    credential_handle: "github-api",
  },
};

const AGENT_PRINCIPAL = { kind: "agent" as const, agent_id: "10000000-0000-4000-8000-000000000001" };

describe("grantsPreviews", () => {
  it("renders kubernetes-domain grant creation in both variants", () => {
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

  it("renders http-domain grant creation and names the domain", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderPreview(
          grantsPreviews.create_grant,
          { grants: [HTTP_ITEM], duration_seconds: 1800, principal: AGENT_PRINCIPAL },
          variant
        )
      ).not.toBeNull();
    }
    expect(
      toolActionDescription(GRANTS_SERVER_ID, "create_grant", {
        grants: [HTTP_ITEM],
        duration_seconds: 1800,
        principal: AGENT_PRINCIPAL,
      })?.text
    ).toBe("Grants: Create HTTP 1 grant");
  });

  it("names an end without distinguishing the caller", () => {
    const args = {
      domain: "kubernetes" as const,
      grant_ids: ["20000000-0000-4000-8000-000000000002", "20000000-0000-4000-8000-000000000003"],
      reason: "probe complete",
    };
    expect(renderPreview(grantsPreviews.revoke_grants, args, "compact")).not.toBeNull();
    expect(toolActionDescription(GRANTS_SERVER_ID, "revoke_grants", args)).toEqual({
      text: "Grants: End Kubernetes 2 grants",
      destructive: true,
    });
  });

  it("uses the same end label when an Operator supplies owner_agent_id", () => {
    const args = {
      domain: "http" as const,
      owner_agent_id: "10000000-0000-4000-8000-000000000001",
      grant_ids: ["20000000-0000-4000-8000-000000000002"],
      reason: "operator revoked",
    };
    expect(renderPreview(grantsPreviews.revoke_grants, args, "detailed")).not.toBeNull();
    expect(toolActionDescription(GRANTS_SERVER_ID, "revoke_grants", args)).toEqual({
      text: "Grants: End HTTP 1 grant",
      destructive: true,
    });
  });

  it("falls back when a grant call does not match its advertised schema", () => {
    expect(renderPreview(grantsPreviews.create_grant, { grants: [] }, "detailed")).toBeNull();
  });
});
