import { expect, test } from "vitest";

import { bindingActions } from "./binding_actions";
import type { BindingView } from "./client";

const RUNTIME: BindingView = {
  name: "demo-asks",
  granted_by: "agent",
  from_git: false,
  subjects: [{ sandbox: "demo", match_labels: null }],
  approval: "pending",
  approved_by: null,
  approved_at: null,
  expires_at: null,
  policies: [],
  missing_policies: [],
  active: false,
  active_reason: "NotApproved",
  active_message: "approval is pending",
};

test("a runtime binding offers revoking and whichever approval it is not already in", () => {
  expect(bindingActions(RUNTIME).map((offer) => offer.action)).toEqual(["approve", "deny", "revoke"]);
  expect(bindingActions({ ...RUNTIME, approval: "approved" }).map((offer) => offer.action)).toEqual(["deny", "revoke"]);
  expect(bindingActions({ ...RUNTIME, approval: "denied" }).map((offer) => offer.action)).toEqual([
    "approve",
    "revoke",
  ]);
  expect(bindingActions(RUNTIME).map((offer) => offer.blocked)).toEqual([null, null, null]);
});

test("a binding from git offers no live action, denial included", () => {
  // Its approval is a field of the manifest, so denying it here would last until the next reconcile.
  const offers = bindingActions({ ...RUNTIME, granted_by: "flux", from_git: true, approval: "approved" });

  expect(offers.map((offer) => offer.action)).toEqual(["deny", "revoke"]);
  expect(offers.every((offer) => offer.blocked?.includes("git"))).toBe(true);
});
