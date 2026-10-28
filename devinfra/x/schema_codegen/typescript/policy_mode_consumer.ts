import { policyModeValues, type PolicyMode } from "./policy_mode.generated";

const typedPolicyModeValues: readonly PolicyMode[] = policyModeValues;

export function defaultPolicyMode(): PolicyMode {
  return typedPolicyModeValues[0];
}

export function allPolicyModes(): readonly PolicyMode[] {
  return typedPolicyModeValues;
}
