import type { Credential } from "../../../egress/api/rules/egress_rules_pb";
import type { PolicyBinding, PolicySet } from "../../../policy/v1/policy_service_pb";

/**
 * The Kubernetes ServiceAccounts a set of bindings lets its subject act as: each egress rule's
 * credential that impersonates one. `complete` is false while a named set or credential is not in
 * view, since what it would reach is then unknown.
 */
export interface KubernetesAccounts {
  accounts: string[];
  complete: boolean;
}

export function kubernetesAccounts(
  bindings: readonly PolicyBinding[],
  sets: ReadonlyMap<string, PolicySet>,
  credentials: ReadonlyMap<string, Credential> | null
): KubernetesAccounts {
  const accounts = new Set<string>();
  let complete = true;
  for (const name of new Set(bindings.flatMap((binding) => binding.spec?.policySets ?? []))) {
    const set = sets.get(name);
    if (!set) {
      complete = false;
      continue;
    }
    for (const rule of set.spec?.rules ?? []) {
      if (rule.type !== "egress" || !rule.credentialRef) continue;
      const credential = credentials?.get(rule.credentialRef.name);
      if (!credential) {
        complete = false;
        continue;
      }
      const account = credential.kubernetesServiceAccount;
      if (account) accounts.add(`${account.namespace}/${account.name}`);
    }
  }
  return { accounts: [...accounts].sort(), complete };
}
