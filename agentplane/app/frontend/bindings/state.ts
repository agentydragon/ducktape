import type { BindingView } from "../schema";
import type { BindingStateKind } from "../status_mark";

export interface BindingState {
  kind: BindingStateKind;
  /** The hover's heading. */
  title: string;
  /** One line per fact behind the mark. */
  reasons: string[];
}

/**
 * Whether the readers of a binding accept it as it stands. Each reader that enforces rules of a type in
 * the binding's sets keeps a condition of its own on it, and none is expected in particular: one with
 * nothing of its type to enforce may write none.
 */
export function bindingState(binding: BindingView): BindingState {
  const refusals = [
    ...binding.missing_policy_sets.map((name) => `No PolicySet ${name} in the namespace`),
    ...binding.policy_sets.flatMap((policySet) =>
      policySet.refused ? [`PolicySet ${policySet.name} is refused: ${policySet.refused}`] : []
    ),
    ...binding.conditions.flatMap((condition) =>
      condition.status === "False" ? [`${condition.type}: ${condition.message || condition.reason}`] : []
    ),
  ];
  if (refusals.length > 0) return { kind: "refused", title: "Refused", reasons: refusals };

  const waiting = [
    ...(binding.conditions.length === 0 ? ["No reader has judged this binding yet"] : []),
    ...binding.conditions.flatMap((condition) =>
      condition.status === "Unknown"
        ? [`${condition.type}: ${condition.message || condition.reason}`]
        : condition.observed_generation !== binding.generation
          ? [
              `${condition.type} judged generation ${condition.observed_generation ?? "?"}; the binding is at ${binding.generation}`,
            ]
          : []
    ),
  ];
  if (waiting.length > 0) return { kind: "pending", title: "Not yet judged", reasons: waiting };

  return {
    kind: "ready",
    title: "Ready",
    reasons: binding.conditions.map((condition) => `${condition.type}: ${condition.message || condition.reason}`),
  };
}
