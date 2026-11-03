import type { PolicyBinding, PolicySet } from "../../../policy/v1/policy_service_pb";
import type { BindingStateKind } from "../status_mark";

export interface BindingState {
  kind: BindingStateKind;
  title: string;
  reasons: string[];
}

/** Conditions belong to policy readers, and only apply to the binding generation they observed. */
export function bindingState(binding: PolicyBinding, sets: ReadonlyMap<string, PolicySet>): BindingState {
  const conditions = binding.status?.conditions ?? [];
  const refusals = [
    ...(binding.spec?.policySets ?? [])
      .filter((name) => !sets.has(name))
      .map((name) => `PolicySet ${name} is not present in the current view`),
    ...conditions
      .filter((condition) => condition.status === "False")
      .map((condition) => `${condition.type}: ${condition.message || condition.reason}`),
  ];
  if (refusals.length > 0) return { kind: "refused", title: "Refused", reasons: refusals };
  const waiting = [
    ...(conditions.length === 0 ? ["No reader has judged this binding yet"] : []),
    ...conditions.flatMap((condition) =>
      condition.status === "Unknown"
        ? [`${condition.type}: ${condition.message || condition.reason}`]
        : condition.observedGeneration !== binding.generation
          ? [
              `${condition.type} judged generation ${condition.observedGeneration || "?"}; the binding is at ${binding.generation}`,
            ]
          : []
    ),
  ];
  if (waiting.length > 0) return { kind: "pending", title: "Not yet judged", reasons: waiting };
  return { kind: "ready", title: "Ready", reasons: conditions.map((c) => `${c.type}: ${c.message || c.reason}`) };
}
