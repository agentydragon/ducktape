import { expect, it } from "vitest";

import { allPolicyModes, defaultPolicyMode } from "./policy_mode_consumer";

it("exports the backend's literal vocabulary as a typed tuple", () => {
  expect(defaultPolicyMode()).toBe("automatic");
  expect(allPolicyModes()).toEqual(["automatic", "manual", "review"]);
});
