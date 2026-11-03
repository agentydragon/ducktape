import { create } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { PolicyBindingSchema, PolicySetSchema } from "../../../policy/v1/policy_service_pb";
import { bindingState } from "./state";

const sets = new Map([
  ["present", create(PolicySetSchema, { identity: { namespace: "policy", name: "present", uid: "1" } })],
]);
function binding(status: string | null, generation = 2n) {
  return create(PolicyBindingSchema, {
    identity: { namespace: "policy", name: "binding", uid: "1" },
    generation: 2n,
    spec: { subject: { namespace: "agents", name: "alice" }, policySets: ["present"] },
    status: {
      conditions:
        status === null
          ? []
          : [
              {
                type: "EgressReady",
                status,
                reason: "Test",
                message: "reader verdict",
                observedGeneration: generation,
              },
            ],
    },
  });
}
describe("bindingState", () => {
  it("waits for a current reader verdict", () => {
    expect(bindingState(binding(null), sets).kind).toBe("pending");
    expect(bindingState(binding("Unknown"), sets).kind).toBe("pending");
    expect(bindingState(binding("True", 1n), sets).kind).toBe("pending");
    expect(bindingState(binding("True"), sets).kind).toBe("ready");
  });
  it("reports refused and missing sets", () => {
    expect(bindingState(binding("False"), sets).kind).toBe("refused");
    expect(bindingState(binding("True"), new Map()).reasons).toContain(
      "PolicySet present is not present in the current view"
    );
  });
});
