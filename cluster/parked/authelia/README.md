# Authelia experiment (paused)

This directory preserves the experimental Authelia deployment for possible
follow-up work. It is intentionally not referenced by the active
`cluster/k8s/kustomization.yaml`, and its Flux Kustomization is removed from the active
bundle, so adding this directory back to the active tree requires an explicit
decision.

`users.yml` is intentionally a non-credential placeholder. Do not restore
plaintext credentials to this directory. Provision a real user database through
the chosen secret-management path, and rotate the old credential from Git
history, before reactivating Authelia.

Authelia is provider-only: it cannot federate upstream to Google or GitHub, so users
authenticate against its local user file or LDAP. That is the key blocker to replacing
Authentik, whose "Sign in with Google" source (`tf/gitops/sso-providers/source_google.tf`)
is live.

Runtime self-service (password reset, profile changes) was rejected: it needs a writable
`users.yml` (a PVC, or an `emptyDir` seeded from the ConfigMap) plus an SMTP notifier,
while this directory mounts `/config` read-only. Password changes would live only in that
volume and drift from the declared config. Revisit only if MFA (TOTP/WebAuthn) device
enrollment needs runtime writes.
