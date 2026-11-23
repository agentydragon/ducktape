# LiteLLM Crossplane migration recovery

## Current recovery state at 2026-10-22 16:07 UTC

All 12 Keys and all three Teams report `Synced=True` and `Ready=True` with the
installed provider package `v2.1.0-1.ducktape.5cc2f4308916`. The LiteLLM provider,
keys, BuildBuddy sync and both Agentplane Flux Kustomizations are Ready.

The remaining Antigravity Team replacement plan came from a `tainted` instance
in the provider Pod's temporary `/tmp/<Team CR UID>/terraform.tfstate`. A
read-only `/team/info` call confirmed the remote Team still existed. Deleting
that Team would also delete its linked Key, because LiteLLM's Team deletion
cascades to associated keys.

The operator permitted Team destruction, but it was unnecessary for this recovery.
A single provider Pod replacement, guarded by its Kubernetes UID, rebuilt the
workspace from the existing managed-resource identity. Antigravity became
`Synced=True` at 16:06:53 UTC. Its CR UID and remote Team UUID stayed unchanged;
all 12 Key CR UIDs and external identifiers also stayed unchanged. No Team or
Key was deleted. The old `LastAsyncOperation=False` condition records the earlier
failed create; the current `Synced=True` condition records successful observation.

This was a one-time recovery of ephemeral provider state, not a change to the
provider's replacement policy. Upjet generates `prevent_destroy` until the managed
resource enters deletion; `managementPolicies: ["*"]` already permits that deletion.
Before deleting a Team, account for LiteLLM's associated-key cascade.

The sections below keep the design and the original failure analysis; the 10:54-14:50 UTC follow-ups are in git history.

## Intended ownership and secret flow

Crossplane is the target owner for LiteLLM Teams and virtual Keys. Keys must be
root-scoped: there is no `Organization` object and no `organizationId` on a Key.
The provider's `default` ProviderConfig gets the in-cluster LiteLLM API base and
master key from an ESO-created Kubernetes Secret. Each Team and Key references
that ProviderConfig. Crossplane connection Secrets expose the generated client
keys; ESO republishes them into consumer namespaces. A separate Tofu root copies
the BuildBuddy live-test key into BuildBuddy and the old Tofu root performs the
handoff cleanup.

Ducktape PR [#11553](https://github.com/agentydragon/ducktape/pull/11553) (merged
13:08 UTC) removed the Organization and all `organizationIdRef`s. It needs a
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

Root-scoped Keys expose two provider bugs. When LiteLLM omits `organization_id`
on read, provider v2.1.0 leaves the optional-computed field unknown, or keeps a
stale known value from state. Separately, `KeyResource.Update` clears `KeyWO`
after Crossplane resolved it from a Kubernetes Secret into the planned state, so
Terraform reports `.key_wo: inconsistent values for sensitive attribute`.
Provider PR [#1](https://github.com/TheCodingSheikh/provider-litellm/pull/1)
resolves only the unknown case; on 2026-10-22 it was open with no released package.

The cluster runs a private build with both fixes (`LITELLM_PROVIDER_PACKAGE` in
[`cluster/cdk8s/crossplane.py`](../cdk8s/crossplane.py); adapter commit in
`cluster/cdk8s/providers/litellm/build_xpkg.sh`). Removing the private pin is
tracked in [`cluster/k8s/TODO.md`](../k8s/TODO.md).

PR #11511 fixed two earlier cutover problems and is merged: it omits
`routerSettings` for Teams without fallbacks, and the ESO export reads the
provider connection field `attribute.key`.
