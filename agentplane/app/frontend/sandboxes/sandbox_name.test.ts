import { expect, it } from "vitest";

import { placeholderSandboxName, sandboxNameError } from "./sandbox_name";

it.each(["a", "box-1", "0x", "a".repeat(57)])("accepts %s", (name) => {
  expect(sandboxNameError(name)).toBeNull();
});

it.each(["", "Upper", "-lead", "trail-", "under_score", "dot.ted", "a".repeat(58)])("refuses %j", (name) => {
  expect(sandboxNameError(name)).not.toBeNull();
});

it("takes the lowest free suffix, filling gaps", () => {
  expect(placeholderSandboxName("dev", new Set())).toBe("dev-1");
  expect(placeholderSandboxName("dev", new Set(["dev-1", "dev-3"]))).toBe("dev-2");
});

it("shortens a long base so the placeholder stays a valid name", () => {
  const name = placeholderSandboxName(`${"a".repeat(56)}-b`, new Set());
  expect(sandboxNameError(name)).toBeNull();
});
