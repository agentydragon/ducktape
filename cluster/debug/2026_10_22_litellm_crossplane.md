# LiteLLM Crossplane migration recovery

## Intended ownership and secret flow

Crossplane is the target owner for LiteLLM Teams and virtual Keys. Keys must be
root-scoped: there is no `Organization` object and no `organizationId` on a Key.
The provider's `default` ProviderConfig gets the in-cluster LiteLLM API base and
master key from an ESO-created Kubernetes Secret. Each Team and Key references
that ProviderConfig. Crossplane connection Secrets expose the generated client
keys; ESO republishes them into consumer namespaces. A separate Tofu root copies
the BuildBuddy live-test key into BuildBuddy and the old Tofu root performs the
handoff cleanup.

Draft ducktape PR [#11553](https://github.com/agentydragon/ducktape/pull/11553)
removes the Organization and all `organizationIdRef`s. It must wait for a
provider package that can reconcile a root-scoped Key through a read cycle.

## Failure causes

The attempted top-level `ducktape` Organization is unavailable on this LiteLLM
deployment. The running process has no `LITELLM_LICENSE`; its organization-info
endpoint returns HTTP 403. Key updates that include `organization_id=ducktape`
then fail because the API database has no such organization. LiteLLM server
logs report: `Organization doesn't exist in db. Organization=ducktape. Create
organization via /organization/new call.` The provider status withholds response
bodies because they may contain the Key dictionary or lookup token.

A trailing-24-hour access-log query at 2026-10-22 10:49 UTC recorded 48
`POST /key/update` responses with HTTP 400 and 53 `GET /organization/info`
responses with HTTP 403. At that check, 8 of 12 Key resources were
`Synced=False`; all 12 still had `organizationId: ducktape`. The missing
organization is the immediate cause of those update failures. Removing the
organization references addresses those 400s.

Root-scoped Keys expose a separate provider bug: when LiteLLM omits
`organization_id` on read, provider v2.1.0 leaves the optional-computed field
unknown. Provider PR
[#1](https://github.com/TheCodingSheikh/provider-litellm/pull/1) resolves only
an unknown omitted value to null and adds a focused regression test. Its body
reports `make test` passing, but the PR is still open with no GitHub checks and
no released package containing the fix. A patched xpkg was built locally from
commit `4c1d445` and its focused regression test passes. Draft ducktape PR #11553
now pins that artifact under `ghcr.io/agentydragon/provider-litellm`, but the
artifact has not been published or pulled by the cluster; do not apply that pin
until publication and pullability are verified. The Crossplane package running
in the cluster remains v2.1.0. The removal of the local package pin is tracked
in [`cluster/k8s/TODO.md`](../k8s/TODO.md).

PR #11511 fixed two earlier cutover problems and is merged: it omits
`routerSettings` for Teams without fallbacks, and the ESO export reads the
provider connection field `attribute.key`.

## Latest live state at 2026-10-22 10:54 UTC

- PR #11511 merged as `ec2baca8c0d9e404e1749e34885c9caa8566b424`.
- Tofu runner fix PR #11564 merged at 10:48 UTC as
  `46453703895e59bba33345fec945830e0dfee13b`. Flux applied that source revision:
  `litellm-keys-tf` became Ready at 10:50:30 UTC and
  `litellm-crossplane-keys` at 10:50:46 UTC. The latter's Ready condition does
  not mean its Keys are Synced; 8 still report `ReconcileError` and 4
  `ReconcileSuccess`.
- `litellm-buildbuddy-sync` has not converged. Its Terraform resource reported
  `TFExecPlanFailed` at 10:52:28 UTC because `buildbuddy-secret-sync` requires
  `TF_VAR_buildbuddy_api_key`. The new manifest correctly provides the
  Secret-backed Terraform var and `BUILDBUDDY_API_KEY` environment value, but
  the live `tofu-controller` still selects runner image
  `ghcr.io/agentydragon/tf-runner:devel-20261022023445-10a3655`. That image's
  source still expects `TF_VAR_buildbuddy_api_key`. The merged commit's
  `push-images / Push tf-runner` job succeeded at 10:52:54 UTC. However, the
  Flux ImageRepository last scanned at 10:52:03 UTC, before that push; the
  ImagePolicy and controller still select the old tag. Verify the new tag is
  scanned, the pin updates, the runner is replaced, and BuildBuddy Tofu
  planning/apply succeeds before calling this fixed.
- Crossplane's installed package remains v2.1.0. Provider PR #1 is still open,
  so the root-scope PR cannot safely be applied yet.
- The live Organization remains `Synced=False`, and all Keys still specify
  `organizationId: ducktape`. The root-scope PR remains a draft pending the
  provider package fix. At 10:54 UTC, the BuildBuddy sync Kustomization remained
  `Unknown/Progressing` (last transition 10:51:13 UTC), while its Terraform
  resource had reported `TFExecPlanFailed` at 10:52:28 UTC.

## Follow-up at 2026-10-22 10:56 UTC

The `tf-runner` ImagePolicy selected the newly published
`devel-20261022105251-4645370` image after image automation pushed pin commit
`2f24ed4e7485860a280dd7c2bbb12a680eb81f73`. The tofu-controller then used that
runner image. `litellm-buildbuddy-sync` Terraform reported
`TerraformAppliedSucceed` at 10:56:23 UTC, and its Flux Kustomization became
Ready at 10:56:24 UTC. A read-only BuildBuddy `ListSecrets` call returned HTTP
200 and confirmed the `OPENAI_API_KEY` name is present; no secret value or
ciphertext was read or printed. The BuildBuddy handoff is now verified.

The LiteLLM Key state has not changed: 8 of 12 Keys remain `Synced=False`, 4 are
`Synced=True`, and all still carry `organizationId: ducktape`. Provider PR #1
remains open, so root-scope adoption is still pending its released package.

After a provider package containing PR #1 is available and pinned, apply the
root-scope change and verify a Key read/reconcile cycle. Then verify all Keys,
exported Secrets, the BuildBuddy Tofu reconciliation, and the stored BuildBuddy
secret. Replacing Keys is acceptable if they cannot be adopted in place; verify
consumer propagation after rotation.

## Follow-up at 2026-10-22 14:04 UTC

The Forgejo image-pull fix merged as ducktape PR #11587 (`77b2ee2e`). Flux
fetched that revision, synced the dedicated `crossplane-system/forgejo-images-creds`
ExternalSecret, installed the provider package, and marked
`crossplane-provider-litellm` Ready. The key Kustomization then applied its new
root-scoped artifact.

Existing Key custom resources still had `spec.forProvider.organizationId: ducktape`,
even though the served artifact omitted that field. A server-side apply dry run
confirmed omission preserves the legacy field; a merge-patch null dry run
confirmed explicit deletion removes it. I applied that deletion to
`key-agentplane-staging` as a one-resource probe. Its spec no longer had the
organization field, but `status.atProvider.organizationId` remained `ducktape`
and its next update still returned HTTP 400. A 15-minute LiteLLM log sample
contained 270 `Organization doesn't exist in db. Organization=ducktape` errors.

The pinned provider patch only resolved an unknown `organization_id` to null.
When the API omitted it but provider state already held the known stale value,
the read path preserved it. The patch is being changed to clear the value on any
API omission, with regression cases for both unknown and stale known state. The
new package version is `v2.1.0-1.ducktape.8fc5d2ef39dd`; its pin and CI build
workflow are in the follow-up change. Local BuildBuddy-backed Bazel testing
could not start because `remote.buildbuddy.io` DNS resolution failed, so PR CI
must provide that validation.

After the patched package is published and installed, remove the stale
`organizationId` from the remaining live Key specs, then verify all Keys report
`Synced=True`, all exported Secrets refresh successfully, and the BuildBuddy
secret sync converges. Do not print or inspect any key values.
