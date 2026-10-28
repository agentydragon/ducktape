import { expect, it } from "vitest";

import type { Decision } from "../schema";
import { groupDecisions } from "./decision_groups";

function decision(at: string, overrides: Partial<Decision> = {}): Decision {
  return {
    at,
    method: "GET",
    host: "api.example.test",
    port: 443,
    outcome: "allow",
    address: null,
    reason: null,
    binding: "test-binding",
    policy: "test-policy",
    rule: 0,
    substituted: false,
    ...overrides,
  };
}

it("groups interleaved requests that were decided the same way, newest member first", () => {
  const groups = groupDecisions([
    decision("2026-01-01T00:00:01Z"),
    decision("2026-01-01T00:00:02Z", { host: "other.example.test" }),
    decision("2026-01-01T00:00:03Z"),
  ]);

  expect(groups.map((group) => [group.host, group.members.length])).toEqual([
    ["api.example.test", 2],
    ["other.example.test", 1],
  ]);
  expect(groups[0].members.map((member) => member.at)).toEqual(["2026-01-01T00:00:03Z", "2026-01-01T00:00:01Z"]);
  expect([groups[0].firstAt, groups[0].lastAt]).toEqual(["2026-01-01T00:00:01Z", "2026-01-01T00:00:03Z"]);
});

it("keeps a refusal apart from an admission of the same destination, and lists it first", () => {
  const groups = groupDecisions([
    decision("2026-01-01T00:00:01Z", { outcome: "deny", policy: null, reason: "no-rule" }),
    decision("2026-01-01T00:00:02Z"),
  ]);

  expect(groups.map((group) => [group.outcome, group.why])).toEqual([
    ["deny", "no-rule"],
    ["allow", "test-policy"],
  ]);
});

it("tells methods, ports and policies apart", () => {
  const groups = groupDecisions([
    decision("2026-01-01T00:00:01Z"),
    decision("2026-01-01T00:00:02Z", { method: "POST" }),
    decision("2026-01-01T00:00:03Z", { port: 8443 }),
    decision("2026-01-01T00:00:04Z", { policy: "other-policy" }),
  ]);

  expect(groups).toHaveLength(4);
});

it("marks a group in which any request had a credential substituted", () => {
  const [group] = groupDecisions([
    decision("2026-01-01T00:00:01Z"),
    decision("2026-01-01T00:00:02Z", { substituted: true }),
    decision("2026-01-01T00:00:03Z"),
  ]);

  expect(group.substituted).toBe(true);
});
