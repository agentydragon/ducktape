import { describe, expect, it } from "vitest";

import { toolActionDescription } from "../actions";
import { renderPreview } from "../entry";
import { KUBERNETES_SERVER_ID } from "../server_ids";
import { kubernetesPreviews } from "./requests";

const GRANT = {
  scope: { kind: "namespaces" as const, namespaces: ["haku-sandbox"] },
  rules: [{ api_groups: [""], resources: ["pods"], verbs: ["get", "list"] }],
};

describe("kubernetesPreviews", () => {
  it("renders grant creation scopes, rules, and duration in both variants", () => {
    for (const variant of ["compact", "detailed"] as const) {
      expect(
        renderPreview(kubernetesPreviews.create_grant, { grants: [GRANT], duration_seconds: 3600 }, variant)
      ).not.toBeNull();
    }
    expect(
      toolActionDescription(KUBERNETES_SERVER_ID, "create_grant", { grants: [GRANT], duration_seconds: 3600 })?.text
    ).toBe("Kubernetes: Create 1 grant");
  });

  it("renders single and batched release calls and marks them destructive", () => {
    expect(
      renderPreview(
        kubernetesPreviews.release_grant,
        { grant_id: "20000000-0000-4000-8000-000000000002", reason: "probe complete" },
        "detailed"
      )
    ).not.toBeNull();
    const args = {
      grant_ids: ["20000000-0000-4000-8000-000000000002", "20000000-0000-4000-8000-000000000003"],
      reason: "probe complete",
    };
    expect(renderPreview(kubernetesPreviews.release_grants, args, "compact")).not.toBeNull();
    expect(toolActionDescription(KUBERNETES_SERVER_ID, "release_grants", args)).toEqual({
      text: "Kubernetes: Release 2 grants",
      destructive: true,
    });
  });

  it("falls back when a grant call does not match its advertised schema", () => {
    expect(renderPreview(kubernetesPreviews.create_grant, { grants: [] }, "detailed")).toBeNull();
  });
});
