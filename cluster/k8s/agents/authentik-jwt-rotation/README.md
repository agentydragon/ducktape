# authentik-jwt-rotation

Hourly CronJob that mints Authentik `client_credentials` JWTs and commits them
SOPS-encrypted to `secrets/`. <rotations.yaml> is the source of truth for
rotation names, providers, output files, credential modes, expected groups /
audiences / claims, and optional in-cluster Secret publication.

`rotate.py` reads each output's unencrypted-by-suffix `expires_unencrypted`
field (no decryption, no in-cluster age key), skips entries with more than
`rotate_below_hours` remaining, and writes everything that actually rotated this
cycle in a single combined commit. The freshness stamp is the final token's own
`exp` claim, so a real mint happens only ~every 44 days per token while a failed
rotation self-heals on the next hourly run.

`expected_audiences` and `expected_claims` are both asserted on every mint
(raising, rather than silently shipping a token missing something a consumer
needs) and both bypass the freshness gate: if the stored token's
`audiences_unencrypted` / `claims_unencrypted` stamp doesn't already satisfy
the current expectation, the rotator re-mints immediately instead of waiting
out the ~44-day expiry. This is what lets an upstream Authentik fix (e.g. a
service account's `email` attribute) roll out on the next hourly run instead
of requiring a manual re-mint.

Each rotation's `credentials_dir` points at a mounted `*-client-credentials`
secret. The default `credential_mode` reads `client_id` + `client_secret`;
`credential_mode: user_password` reads `client_id` + `username` + `password`
for Authentik service-account app-password grants. `proxy_client_id` is also
read when `exchange_scopes` is set. Consumers read the committed JWT via
<../../../../devinfra/k8s/kubeconfig.py> (k8s token) or the Alloy OTLP bearer
flow.

Rotations whose `provider_slug` is `kubectl-sandbox-client-credentials`
deliberately share the issuer/audience that kube-apiserver trusts. Their
effective Kubernetes RBAC comes from the provider's explicit Authentik
machine-principal allowlist, not from adding more apiserver issuer entries.

The image bakes `rotate.py` (Python interpreter via `py_image_layer`), the
`sops` binary, and `git` + `ca-certificates`. The per-deployment YAML list rides
in as a configMap so adding a rotation needs no image rebuild.

Deviation from the sibling `attic-jwt-rotation`: that one mints via `atticadm`
exec inside the attic pod (a different mechanism and namespace) and stays
separate; this one is purely Authentik OAuth.
