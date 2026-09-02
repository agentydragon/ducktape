# Authelia experiment (paused)

This directory preserves the experimental Authelia deployment for possible
follow-up work. It is intentionally not referenced by the active
`cluster/k8s/kustomization.yaml`; the local Flux Kustomizations are also
suspended so adding this directory back to the active tree requires an explicit
decision.

`app/users.yml` is intentionally a non-credential placeholder. Do not restore
plaintext credentials to this directory. Provision a real user database through
the chosen secret-management path, and rotate the old credential from Git
history, before reactivating Authelia.
