import type { Identity, PolicyBinding } from "../../../policy/v1/policy_service_pb";

/**
 * The bindings naming an identity, split by what a delete does to them. An identity's own bindings
 * (owner reference to its UID) are garbage-collected with it. Any other binding names its subject
 * by name only, so it would grant a later identity of the same name: it references the identity,
 * and the Policy Controller refuses the delete while one does.
 */
export interface IdentityBindings {
  own: PolicyBinding[];
  referencing: PolicyBinding[];
}

export function identityBindings(identity: Identity, bindings: readonly PolicyBinding[]): IdentityBindings {
  const name = identity.identity?.name;
  const uid = identity.identity?.uid;
  const naming = bindings.filter((binding) => binding.spec?.subject?.identity?.name === name);
  const owned = (binding: PolicyBinding): boolean => binding.ownerReferences.some((owner) => owner.uid === uid);
  return {
    own: naming.filter(owned),
    referencing: naming.filter((binding) => !owned(binding)),
  };
}
