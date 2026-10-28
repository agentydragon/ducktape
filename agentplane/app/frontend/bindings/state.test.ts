import { describe, expect, it } from "vitest";

import type { BindingView } from "../schema";
import { bindingState } from "./state";

function binding(overrides: Partial<BindingView>): BindingView {
  return {
    name: "test-binding",
    provenance: "operator",
    generation: 2,
    subjects: [{ namespace: "test-namespace", name: "test-account" }],
    expires_at: null,
    conditions: [],
    policy_sets: [],
    missing_policy_sets: [],
    ...overrides,
  };
}

function verdict(type: string, status: "True" | "False" | "Unknown", observedGeneration: number | null) {
  return {
    type,
    status,
    reason: "TestReason",
    message: `${type} test message`,
    observed_generation: observedGeneration,
  };
}

describe("bindingState", () => {
  it("is pending until some reader has judged the binding", () => {
    expect(bindingState(binding({})).kind).toBe("pending");
  });

  it("is ready once every reader that judged it accepts its current generation", () => {
    const state = bindingState(
      binding({ conditions: [verdict("ActionsReady", "True", 2), verdict("EgressReady", "True", 2)] })
    );

    expect(state.kind).toBe("ready");
  });

  it("is pending while a verdict is of an older generation or not yet decided", () => {
    expect(bindingState(binding({ conditions: [verdict("EgressReady", "True", 1)] })).kind).toBe("pending");
    expect(bindingState(binding({ conditions: [verdict("EgressReady", "Unknown", 2)] })).kind).toBe("pending");
  });

  it("is refused when any reader says False, even beside readers that accept it", () => {
    const state = bindingState(
      binding({ conditions: [verdict("ActionsReady", "True", 2), verdict("EgressReady", "False", 2)] })
    );

    expect(state.kind).toBe("refused");
    expect(state.reasons.join("\n")).toContain("EgressReady test message");
    expect(state.reasons.join("\n")).not.toContain("ActionsReady test message");
  });

  it("is refused for a PolicySet that is missing or refused, whatever the readers last said", () => {
    const accepted = [verdict("EgressReady", "True", 2)];

    expect(bindingState(binding({ conditions: accepted, missing_policy_sets: ["gone"] })).kind).toBe("refused");
    expect(
      bindingState(
        binding({
          conditions: accepted,
          policy_sets: [{ name: "broken", generation: 1, rules: [], refused: "rules[0]: test parse failure" }],
        })
      ).kind
    ).toBe("refused");
  });
});
