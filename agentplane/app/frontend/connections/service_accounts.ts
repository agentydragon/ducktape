import type { ServiceAccount } from "../../../proto/identity_pb";

/** `namespace/name`, as kubectl spells a ServiceAccount; the key a picker selects by. */
export function serviceAccountKey(account: ServiceAccount): string {
  return `${account.namespace}/${account.name}`;
}

export function isEligibleCaller(caller: ServiceAccount, accounts: ServiceAccount[]): boolean {
  return accounts.some((account) => serviceAccountKey(account) === serviceAccountKey(caller));
}
