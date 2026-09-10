# Crossplane and Kubernetes-secret OpenTofu audit

This records the September 2026 inventory taken while deciding whether
Crossplane can replace the GitOps OpenTofu roots. It is a design note, not a
migration instruction: no root should be removed until its replacement is
ready, reconciled, and its consumers have passed an authenticated check.

## Crossplane coverage

The cluster does not currently install Crossplane. Of the 22 `tf/gitops`
roots, only two external APIs have a practical Crossplane path:

| Terraform resources                                                                                                                           | Roots                                                                                                                     | Assessment                                                                                                                                                                                               |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `aws_route53_record`                                                                                                                          | `dns-records`                                                                                                             | A supported Crossplane Route53 `Record` resource exists. `aws_route53domains_registered_domain` has no equivalent in that provider's published resource list, so this root cannot move as a unit.        |
| `github_actions_secret`, `github_actions_variable`, `github_repository_environment`, `github_repository_ruleset`, `github_repository_webhook` | `github-secrets-sync`, `github-branch-protection`, `flux-webhook-token`                                                   | The community `provider-upjet-github` has equivalents, but its managed-resource APIs are `v1alpha1`. Treat the three roots as one optional migration after a provider and credential-scoping evaluation. |
| `forgejo_*`                                                                                                                                   | `augur-evidence`, `budget-ledger`, `cpap-data`, `forgejo-agentydragon*`, `forgejo-claude`, `forgejo-images`, `haku-state` | No maintained Crossplane provider was identified.                                                                                                                                                        |
| `authentik_*`                                                                                                                                 | `agent-machine-access`, `alloy-otlp-bearer-token`, `gatus-sso`, `sso-providers`                                           | No maintained Crossplane provider was identified.                                                                                                                                                        |
| `litellm_*`, `claude-managed-agents_*`                                                                                                        | `litellm-keys`, `haku-cloud-agent`                                                                                        | No usable Crossplane provider was identified.                                                                                                                                                            |

Installing Crossplane's OpenTofu/Terraform provider would not solve the
tofu-controller runner-RBAC problem. It would run the same abstraction behind
another controller, while retaining provider credentials and Terraform state.

## Kubernetes-only roots

Three roots use OpenTofu principally to mint or reshape Kubernetes Secrets.
They are candidates to retire from OpenTofu, but **ESO is not an external
secret-manager replacement in this cluster**. The standing design is SOPS as
the secret source of truth; ESO's Kubernetes provider only copies or renders
those in-cluster source Secrets. See `cluster/docs/decisions.md` under
"Secrets: SOPS SSOT".

### `ollama-bearer-token`

Current shape:

- `random_password.bearer_token` persists a 48-character value in Terraform
  state.
- `kubernetes_secret_v1_data` writes `ollama/ollama-bearer-token:token`.
- `ollama`'s nginx sidecar consumes it as `OLLAMA_DIRECT_TOKEN`.
- Emberstack Reflector mirrors it to `claude-sandbox`; the source-object
  annotations are in `cluster/k8s/ollama/secrets/ollama-bearer-token.yaml`.

This can be replaced by the exact native ESO pattern already used for the
OpenClaw gateway passwords:

```yaml
apiVersion: generators.external-secrets.io/v1alpha1
kind: Password
metadata:
  name: ollama-bearer-token-generator
  namespace: ollama
spec:
  length: 48
  digits: 12
  symbols: 0
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: ollama-bearer-token
  namespace: ollama
spec:
  refreshInterval: 8760h
  target:
    name: ollama-bearer-token
    creationPolicy: Owner
    deletionPolicy: Retain
    template:
      metadata:
        annotations:
          reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
          reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces: claude-sandbox
          reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
          reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: claude-sandbox
      data:
        token: "{{ .password }}"
  dataFrom:
    - sourceRef:
        generatorRef:
          apiVersion: generators.external-secrets.io/v1alpha1
          kind: Password
          name: ollama-bearer-token-generator
```

The proof is on disk: `cluster/k8s/agents/public-coder-agent/app/gateway-password-eso.yaml`
and `cluster/k8s/agents/haku-openclaw-spike/app/gateway-password-eso.yaml` use
this same `Password` plus `ExternalSecret` pattern, with a retained target and
a one-year refresh interval. The live `Password`, `ExternalSecret`, and
`SecretStore` CRDs are installed; on 2026-09-09 the public-coder gateway
`ExternalSecret` reported `Ready=True, SecretSynced`.

This is a deliberate token rotation. ESO's Password generator cannot import
the token held only in Terraform state. The safe sequence is to deploy the new
objects, let workloads and the reflected client Secret converge, test an
authenticated direct Ollama request, and only then remove the Terraform CR and
state. If retaining the exact current token matters, put it in a new
SOPS-encrypted Secret instead of using a generator.

### `litellm-api-key`

This root creates three Secrets from two generated values:

- `litellm/litellm-master-key:api-key`, reflected to `claude-sandbox` and
  `props`, and a differently-keyed `gatus/litellm-api-key:LITELLM_API_KEY`.
- `litellm/litellm-salt-key:key`.

The master key can be rotated, but the salt key explicitly cannot: LiteLLM uses
it to encrypt credential material already stored in its database. A generated
ESO password is therefore inappropriate for this root as a whole. The correct
replacement is two SOPS-encrypted source Secrets, deployed by Flux, preserving
the current values:

1. Create `litellm-master-key.sops.yaml` and `litellm-salt-key.sops.yaml` in
   `cluster/k8s/litellm/secrets/` with the existing key names and values.
2. Retain the master-key reflection annotations and add `gatus` as a consumer.
3. Change Gatus from `envFrom: litellm-api-key` to an explicit
   `LITELLM_API_KEY` `secretKeyRef` for `litellm-master-key:api-key`; this
   removes the duplicate Secret rather than duplicating an API key ciphertext.
4. Confirm the LiteLLM deployment, Gatus, and reflected consumers use the
   preserved value. Only then remove `litellm-api-key`'s Terraform CR and
   state.

The source must be SOPS rather than a Password generator because preserving
`litellm-salt-key` is an invariant. ESO may still distribute a SOPS source
Secret cross-namespace, but it does not make the source value durable by
itself.

### `gaffer-private-ghcr-pull`

This is not a random value. The source is already a SOPS Secret:
`flux-system/github-pat-ghcr-read:token`. OpenTofu merely transforms it into
the `kubernetes.io/dockerconfigjson` Secret `flux-system/gaffer-ghcr-pull` and
Reflector fans it out to `augur`, `listing-monitor`, and `thrive-scraper`.

ESO can render that target without OpenTofu. Use a namespaced `SecretStore` in
`flux-system`, authenticated by a dedicated ServiceAccount whose Role grants
only `get` on `github-pat-ghcr-read`, then an `ExternalSecret` that reads the
PAT and templates `.dockerconfigjson`. Keep the existing Reflector annotations
on the target. This is an established least-privilege pattern in
`cluster/k8s/agents/public-coder-agent/backup/repository-secret-store.yaml`:
it uses a ServiceAccount, a Role with `resourceNames`, and a same-namespace
Kubernetes `SecretStore` to compose a target Secret.

That exact `SecretStore` was also live and `Ready=True, Valid` on 2026-09-09.

The proposed target needs no generated state and does not rotate the PAT. The
cutover checks are: ESO `Ready=True`, the source and each reflected pull Secret
exist with Docker config type, and a newly scheduled private-image Pod can pull
in every consumer namespace. Then remove the Terraform CR and state.

## Recommended order

1. Move `gaffer-private-ghcr-pull` first. It is a pure SOPS-to-ESO rendering
   change and has an exact in-repository least-privilege template.
2. Move `ollama-bearer-token` next only as an explicitly tested rotation, or
   preserve it through SOPS if rotation is undesirable.
3. Move `litellm-api-key` as a SOPS preservation migration, including the
   no-rotation salt-key check. Do not treat it as a disposable random secret.
4. Revisit Crossplane only after those removals: Route53 records are the first
   supported-provider pilot; GitHub is a separate community-provider decision.
