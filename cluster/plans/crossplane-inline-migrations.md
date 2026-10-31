# Crossplane provisioning migration

Umbrella tracker: [#5943](https://github.com/agentydragon/ducktape/issues/5943).

## Remaining tofu-controller roots

| Root                      | Required work                                                                                                                                                                     | Scheduling                                                          |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `thrive-scrape`           | Add private manifest generation and per-root runtime/RBAC; preserve the existing state schema and credentials during ownership handoff. Keep source and Inline manifests private. | Next ordinary application candidate; source investigation complete. |
| `home-switch`             | Preserve human approval of a specific plan, home-network runtime placement and CA mount.                                                                                          | Design the approval/runtime workflow before migration.              |
| `infra-drift`             | Preserve targeted, plan-only OVH drift checks and handle local modules, files and provisioners.                                                                                   | Separate source and drift-reporting design.                         |
| `gitlab-spike`            | Resolve generated JSON configuration/dependencies through a native supported source path, without hand-written replacement HCL.                                                   | Explicitly postponed; tofu-controller ownership restored by #12133. |
| `litellm-buildbuddy-sync` | Review the live-test credential sync separately.                                                                                                                                  | Explicitly skipped by the operator for now.                         |

The bootstrap root under `cluster/terraform` has a separate lifecycle; it is not
implicitly part of this in-cluster controller migration.

## Remaining cross-cutting work

- Audit the isolation boundary. Per-root impersonated Kubernetes identities scope ordinary
  API requests, but the shared provider process is not an execution sandbox. Shared
  backend and credential access still need an explicit isolation assessment.
- Resolve or accept [#12157](https://github.com/agentydragon/ducktape/issues/12157):
  existing-state adoption can persist `Synced=True` on the first reconcile but delay
  `Ready` and checksum until the next drift poll (roughly ten minutes). Evidence points
  to [crossplane-runtime#791](https://github.com/crossplane/crossplane-runtime/issues/791);
  the proposed [#804](https://github.com/crossplane/crossplane-runtime/pull/804) was closed
  unmerged. Missing `Ready` does not establish that planning/application has not happened.
- Decide the migration boundary for exceptional roots before retiring tofu-controller.
  Remove controller, runner, RBAC and source plumbing only once no live roots depend on
  them.

## Handoff and cleanup gates

Active Workspaces use `deletionPolicy: Orphan` and Observe/Create/Update management
policies. Deleting a Workspace preserves managed infrastructure; changing HCL can still
delete resources. Flux health checks require `Ready=True` and current-generation
`Synced=True`.

For each remaining migration:

1. Establish the root's source packaging, runtime identity and necessary grants.
2. Stop the old state writer before enabling the replacement; preserve the PostgreSQL
   schema, `default` workspace and resource addresses.
3. Verify replacement readiness and intended resource changes before retiring the old
   Terraform object's prune gate and source artifact.
4. Set and verify the outer Flux gate's own `deletionPolicy: Orphan` before removing it.
   Removing an empty parent root can expose nested manifests to recursive discovery;
   see [the lifecycle warning](../docs/cdk8s.md). Workspace Orphan policy alone does not
   stop Flux from deleting the Workspace object.

To return ownership after a failed handoff, pause Crossplane reconciliation, remove the
Workspace with Orphan policy, verify it no longer owns the state, then restore the old
Terraform object. Never run both writers concurrently.

Completed migration gates and obsolete per-root artifacts have been removed, including
GitHub cleanup in #12148 / #12149. Keep the shared `authentik-tf`, `forgejo-gitops` and
`github-tf` artifacts while nested Workspace Kustomizations consume them. The
`litellm-keys-tf` Kustomization still applies the credential used by
`litellm-buildbuddy-sync`; it is not an obsolete gate.
