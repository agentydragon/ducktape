# GitHub Secrets Sync PAT

`github-secrets-sync-pat` is a fine-grained GitHub PAT. Its canonical encrypted
Secret is `external-creds/github-agentydragon.sops.yaml` in `ducktape-flux`.
External Secrets Operator creates namespace-local `github-secrets-sync-pat`
copies for the approved consumers.

The token should be scoped to selected repositories:

- `agentydragon/ducktape`
- `agentydragon/gaffer-private`

Required repository permissions:

| Permission     | Access     | Why                                                                                                                                                                                                                                                                                                             |
| -------------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Contents       | Read/write | The token is mounted into the Authentik and Attic token-rotation CronJobs, which sparse-clone `ducktape`, edit SOPS files, commit, and push to `devel`.                                                                                                                                                         |
| Secrets        | Read/write | `tf/gitops/github-secrets-sync` manages the `SOPS_AGE_KEY` GitHub Actions secret for both repositories, Forgejo registry credentials for both, and `DUCKTAPE_AUTOMATION_PRIVATE_KEY`, `BUILDBUDDY_API_KEY`, `REPO_TOKEN`, `ATTIC_TOKEN`, and the DriveFS reader pair for `gaffer-private`; it also manages Ducktape's Attic reader/writer tokens, BuildBuddy key, and PR visual S3 credentials. |
| Environments   | Read/write | `tf/gitops/github-secrets-sync` manages the protected `fork-ci-review` environment for explicitly approved non-agent fork revisions.                                                                                                                                                                            |
| Variables      | Read/write | `tf/gitops/github-secrets-sync` manages the `PROPS_REGISTRY_URL` GitHub Actions variable for `ducktape`. GitHub's fine-grained permission header calls this `actions_variables`; without it, GitHub returns `403 Resource not accessible by personal access token`.                                             |
| Administration | Read/write | `tf/gitops/github-branch-protection` manages the `ducktape` repository ruleset for branch protection. GitHub lists repository ruleset endpoints under the `Administration` permission.                                                                                                                          |
| Webhooks       | Read/write | `tf/gitops/flux-webhook-token` manages the `ducktape` repository webhook used by the Flux GitHub receiver.                                                                                                                                                                                                      |

`Metadata: read` is implicit for fine-grained PATs.

This shared PAT is deliberately broad because it backs several small GitOps
modules and token-rotation jobs. If those ownership boundaries need to tighten,
split this into purpose-specific PATs instead of removing individual permissions
from the shared token.

The source grants explicitly approve `flux-system`, `agents-infra`, and
`nix-cache`. Each namespace owns its `external-creds-reader` ServiceAccount and
ExternalSecret; Reflector is not part of this credential's distribution path.

`PROPS_REGISTRY_URL` existed in GitHub before Terraform owned it, so
`tf/gitops/github-secrets-sync/main.tf` includes an import block for
`ducktape:PROPS_REGISTRY_URL`. Keep that import block until the resource is
present in the `github_secrets_sync` remote state.

The `FORGEJO_IMAGES_USERNAME` and `FORGEJO_IMAGES_PASSWORD` secrets for
`ducktape` and `gaffer-private` come from the canonical `forgejo-images/forgejo-images-creds`
Secret. Do not copy those values into GitHub manually. The Terraform reads that
Secret through `data "kubernetes_secret"`; until it exists the plan fails and
the Crossplane Workspace retries.

If the `github-secrets-sync` Workspace starts failing, check the
accepted-permission header for the failing endpoint before broadening the token:

```bash
TOKEN=$(kubectl -n flux-system get secret github-secrets-sync-pat \
  -o jsonpath='{.data.token}' | base64 -d)

curl -sS -D - -o /tmp/github-body \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/agentydragon/ducktape/actions/variables/PROPS_REGISTRY_URL \
  | grep -i '^x-accepted-github-permissions:'
```

The Attic rotator publishes separate reader and writer Secrets:
`nix-cache/attic-ducktape-ci-reader`, `nix-cache/attic-ducktape-ci-writer`,
and `nix-cache/attic-gaffer-ci-writer`. It patches the
`github-secrets-sync` Workspace's token-version annotations after each
successful token check. The annotations carry Secret identities and versions,
never token contents. Annotation changes enqueue the pinned provider's Workspace
reconciler, which reads the corresponding Secret and updates Ducktape's reader
and writer secrets or gaffer's `ATTIC_TOKEN`. Periodic reconciliation remains a
recovery path.

All three Secrets are declared without data so Flux owns their identities and
the rotator owns their token data. Its RBAC names those Secrets and the shared
Workspace; the sync Workspace has read access to each Secret. Gaffer's writer
token stays mirrored to its existing SOPS file during the consumer cutover.
On first deployment, the gaffer Actions-secret data source is unavailable until
the rotator has written `attic-gaffer-ci-writer`; the Workspace retries until
that first rotation succeeds. Verify the mirrored Secret and Actions secret
before removing any SOPS recipient or source.

The reader token can pull from `main` and `gaffer` but cannot push. Workflows
that only substitute from Attic use `ATTIC_READ_TOKEN`; upload jobs use
`ATTIC_TOKEN` only on runs that push to Attic.

The `DUCKTAPE_AUTOMATION_PRIVATE_KEY` Actions secret comes from the existing
`flux-system/ducktape-automation-github-app` Secret's `githubAppPrivateKey`.
Flux decrypts that SOPS-managed Secret; OpenTofu owns the Actions copy.
Provision and verify it before switching `mint-automation-token` away from CI
SOPS decryption. Retain the CI recipient until all remaining consumers migrate.
