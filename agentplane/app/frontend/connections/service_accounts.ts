import type { ServiceAccount } from "../../../proto/identity_pb";

/** `namespace/name`, as kubectl spells a ServiceAccount, for retained Action attribution. */
export function serviceAccountKey(account: ServiceAccount): string {
  return `${account.namespace}/${account.name}`;
}
