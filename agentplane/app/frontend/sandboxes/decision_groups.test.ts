import { expect, it } from "vitest";
import { Decision_Outcome, Decision_Reason, type Decision } from "../../../egress/api/admin/egress_admin_pb";
import { decisionFixture } from "./decision_fixture";
import { decisionAt, decisionOutcome, decisionReason, groupDecisions } from "./decision_groups";

function decision(at: string, overrides: Omit<Partial<Decision>, "at"> = {}): Decision {
  return decisionFixture({
    at,
    host: "api.example.test",
    binding: "test-binding",
    policy: "test-policy",
    rule: 0,
    ...overrides,
  });
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
  expect(groups[0].members.map(decisionAt)).toEqual(["2026-01-01T00:00:03Z", "2026-01-01T00:00:01Z"]);
  expect([groups[0].firstAt, groups[0].lastAt]).toEqual(["2026-01-01T00:00:01Z", "2026-01-01T00:00:03Z"]);
});

it("keeps a refusal apart from an admission of the same destination, and lists it first", () => {
  const groups = groupDecisions([
    decision("2026-01-01T00:00:01Z", {
      outcome: Decision_Outcome.DENY,
      policy: undefined,
      reason: Decision_Reason.NO_RULE,
    }),
    decision("2026-01-01T00:00:02Z"),
  ]);

  expect(groups.map((group) => [decisionOutcome(group.members[0]), group.why])).toEqual([
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

it("keeps an empty log empty and keeps an unspecified outcome distinct", () => {
  expect(groupDecisions([])).toEqual([]);
  const [group] = groupDecisions([
    decision("2026-01-01T00:00:01Z", {
      outcome: Decision_Outcome.UNSPECIFIED,
      policy: undefined,
      reason: Decision_Reason.UNKNOWN,
    }),
  ]);
  expect(decisionOutcome(group.members[0])).toBe("unknown");
  expect(group.why).toBe("unknown");
});

it("spells a refusal as the proxy's header does, and one the proxy could not name as unknown", () => {
  const refused = (reason: Decision_Reason) =>
    decision("2026-01-01T00:00:01Z", { outcome: Decision_Outcome.DENY, policy: undefined, reason });

  expect(decisionReason(refused(Decision_Reason.PLACEHOLDER_UNRESOLVED))).toBe("placeholder-unresolved");
  expect(decisionReason(refused(Decision_Reason.UNKNOWN))).toBe("unknown");
});
