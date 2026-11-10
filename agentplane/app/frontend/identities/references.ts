import type { Identity, PolicyBinding } from "../../../policy/v1/policy_service_pb";
import type { EnvironmentDescriptor } from "../../../environments/v1/environment_service_pb";
import type { EnvironmentTarget } from "../../../proto/identity_pb";
import type { Session } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";

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

/** References the Sandbox delete will leave behind. The selected Sandbox itself is going away. */
export function remainingIdentityReferences(
  identity: Identity,
  bindings: readonly PolicyBinding[],
  sandboxes: readonly EnvironmentDescriptor[],
  sessions: readonly Session[],
  deleting: EnvironmentTarget | null
): string[] {
  const name = identity.identity?.name;
  if (!name) return ["Identity name is unavailable"];
  const references = identityBindings(identity, bindings).referencing.map(
    (binding) => `PolicyBinding ${binding.identity?.name ?? "(unnamed)"}`
  );
  for (const sandbox of sandboxes) {
    if (sandbox.identityName !== name) continue;
    const target = sandbox.target;
    if (!target?.namespace || !target.name || !target.uid) {
      references.push("A Sandbox with an incomplete identity");
    } else if (
      !deleting ||
      target.namespace !== deleting.namespace ||
      target.name !== deleting.name ||
      target.uid !== deleting.uid
    ) {
      references.push(`Sandbox ${target.namespace}/${target.name}`);
    }
  }
  // Dispatcher retains Sessions independently of Sandbox deletion. Even a Session placed on the
  // selected Sandbox remains a reference, so a cleanup choice is withheld until it is retired.
  for (const session of sessions) {
    if (session.identity?.name === name) references.push(`Session ${session.sessionId}`);
  }
  return references;
}
