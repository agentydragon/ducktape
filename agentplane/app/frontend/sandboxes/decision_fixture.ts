import type { Decision } from "./client";

/** Shared test data for the current browser decision stream. */
export type DecisionFixture = Omit<Partial<Decision>, "at" | "host"> & Pick<Decision, "at" | "host">;

export function decisionFixture({ at, host, ...fields }: DecisionFixture): Decision {
  return {
    at,
    host,
    method: "GET",
    port: 443,
    outcome: "allow",
    address: null,
    reason: null,
    binding: null,
    policy: null,
    rule: null,
    substituted: false,
    ...fields,
  };
}
