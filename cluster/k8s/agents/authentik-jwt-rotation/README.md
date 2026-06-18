# authentik-jwt-rotation

Hourly CronJob that mints Authentik `client_credentials` JWTs and commits them
SOPS-encrypted to `secrets/`. One job rotates every token in
<rotations.yaml>:

| Rotation         | Provider                             | Output                                 | Notes                                 |
| ---------------- | ------------------------------------ | -------------------------------------- | ------------------------------------- |
| `claude-web-k8s` | `kubectl-sandbox-client-credentials` | `secrets/claude-web-k8s-jwt.yaml`      | provider secret; group check          |
| `haku-k8s`       | `kubectl-sandbox-client-credentials` | `secrets/haku-k8s-jwt.yaml`            | user/password app-token; group `haku` |
| `alloy-otlp`     | `alloy-otlp-client-credentials`      | `secrets/alloy-otlp-bearer-token.yaml` | provider secret; proxy exchange       |

`rotate.py` reads each output's unencrypted-by-suffix `expires_unencrypted`
field (no decryption, no in-cluster age key), skips entries with more than
`rotate_below_hours` remaining, and writes everything that actually rotated this
cycle in a single combined commit. The freshness stamp is the final token's own
`exp` claim, so a real mint happens only ~every 44 days per token while a failed
rotation self-heals on the next hourly run.

Each rotation's `credentials_dir` points at a mounted `*-client-credentials`
secret. The default `credential_mode` reads `client_id` + `client_secret`;
`credential_mode: user_password` reads `client_id` + `username` + `password`
for Authentik service-account app-password grants. `proxy_client_id` is also
read when `exchange_scopes` is set. Consumers read the committed JWT via
<../../../../devinfra/k8s/kubeconfig.py> (k8s token) or the Alloy OTLP bearer
flow.

`claude-web-k8s` and `haku-k8s` deliberately share the same
`kubectl-sandbox-client-credentials` issuer/audience that kube-apiserver trusts.
Their effective Kubernetes RBAC comes from the provider's explicit Authentik
machine-principal allowlist, not from adding more apiserver issuer entries.

The image bakes `rotate.py` (Python interpreter via `py_image_layer`), the
`sops` binary, and `git` + `ca-certificates`. The per-deployment YAML list rides
in as a configMap so adding a rotation needs no image rebuild.

Deviation from the sibling `attic-jwt-rotation`: that one mints via `atticadm`
exec inside the attic pod (a different mechanism and namespace) and stays
separate; this one is purely Authentik OAuth.
