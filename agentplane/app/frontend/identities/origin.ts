import type { Identity } from "../../../policy/v1/policy_service_pb";

/**
 * Where an identity came from, as far as its object says. Flux labels what it applies from git.
 * Nothing yet records whether the app or a Session's Dispatcher created an identity, so every other
 * identity is `unknown`.
 */
export type IdentityOrigin = "git" | "unknown";

export function identityOrigin(identity: Identity): IdentityOrigin {
  return "kustomize.toolkit.fluxcd.io/name" in identity.labels ? "git" : "unknown";
}
