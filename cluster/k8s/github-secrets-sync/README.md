# GitHub Secrets Sync PAT

`github-secrets-sync-pat` is a fine-grained GitHub PAT stored as a SOPS-managed
Kubernetes Secret in `flux-system`.

The token should be scoped to selected repositories:

- `agentydragon/ducktape`
- `agentydragon/gaffer-private`

Required repository permissions:

| Permission     | Access     | Why                                                                                                                                                                                                                                                                             |
| -------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Contents       | Read/write | The token is mounted into the Authentik and Attic token-rotation CronJobs, which sparse-clone `ducktape`, edit SOPS files, commit, and push to `devel`.                                                                                                                         |
| Secrets        | Read/write | `tf/gitops/github-secrets-sync` manages the `SOPS_AGE_KEY` GitHub Actions secret for `ducktape` and `gaffer-private`, plus the narrow `BUILDBUDDY_API_KEY` and PR visual S3 credentials for `ducktape` CI.                                                                      |
| Environments   | Read/write | `tf/gitops/github-secrets-sync` manages the protected `fork-ci-review` environment for explicitly approved non-agent fork revisions.                                                                                                                                            |
| Variables      | Read/write | `tf/gitops/github-secrets-sync` manages the `PROPS_REGISTRY_URL` GitHub Actions variable for `ducktape`. GitHub's fine-grained permission header calls this `actions_variables`; without it, tofu-controller fails with `403 Resource not accessible by personal access token`. |
| Administration | Read/write | `tf/gitops/github-branch-protection` manages the `ducktape` repository ruleset for branch protection. GitHub lists repository ruleset endpoints under the `Administration` permission.                                                                                          |
| Webhooks       | Read/write | `tf/gitops/flux-webhook-token` manages the `ducktape` repository webhook used by the Flux GitHub receiver.                                                                                                                                                                      |

`Metadata: read` is implicit for fine-grained PATs.

This shared PAT is deliberately broad because it backs several small GitOps
modules and token-rotation jobs. If those ownership boundaries need to tighten,
split this into purpose-specific PATs instead of removing individual permissions
from the shared token.

The token is reflected from `flux-system` into `agents-infra` and `nix-cache`
because the JWT rotation CronJobs consume it there.

`PROPS_REGISTRY_URL` existed in GitHub before Terraform owned it, so
`tf/gitops/github-secrets-sync/main.tf` includes an import block for
`ducktape:PROPS_REGISTRY_URL`. Keep that import block until the resource is
present in tofu-controller's remote state.

If `github-secrets-sync` starts failing, check the accepted-permission header
for the failing endpoint before broadening the token:

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
