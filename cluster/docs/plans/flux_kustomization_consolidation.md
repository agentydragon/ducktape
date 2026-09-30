# Flux Kustomization consolidation

Bring the existing graph to <../flux_kustomization_policy.md>. Counts are from the
generated chart at devel `a959d0eb97` (2026-09-28): 169 Kustomizations, 241
`dependsOn` edges, longest chain 5.

Batches are independently landable and mostly independently reviewable; the
ordering below is by risk, not by dependency. Anything touching a CNPG
`Cluster`, PVC, `Bucket`, `S3Identity` or `Terraform` CR follows <../../AGENTS.md>
§ Migrating stateful Flux Kustomizations — suspend, `prune: false`,
`deletionPolicy: Orphan`, verify identity, then cut over.

## 1. Drop ungated `wait` / `healthChecks`

105 unsuspended Kustomizations set `wait: true` or `healthChecks` with nothing
depending on them. `flux.flux_kustomization` defaults `wait=True`: flip that
default to unset, keep `wait` on the nodes something depends on, and regenerate.
No resource moves.

## 2. Split CRDs out of the webhookless operators

`monitoring-crds` already carries the prometheus-operator CRDs, and
`OPERATOR_CRDS` points the monitor kinds at it. What remains buys resilience (a
sick operator stops blocking its consumers' applies), not chain depth.

Eligible — no admission webhook over their CRs, confirmed 2026-09-16:
`seaweedfs-operator` (18 dependents), `tofu-controller` (14), `valkey` (7). Each
is Helm-installed, so each split is real work: `crds.enabled=false` or the
chart's equivalent, plus a `<operator>-crds` Kustomization sourcing them. Take
them in order and stop where the consumer count stops paying for it.

Not eligible: ESO, `cnpg`, `cert-manager`, `kyverno`, `kubevirt`/`virt-api`,
`cdi` all register `failurePolicy: Fail` webhooks. Their consumers depend on the
operator, and that edge is class 1. Re-check with
`kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations`
before moving one; charts add and remove webhooks across versions.

## 3. Central `namespaces` Kustomization — multi-tenant Namespaces only

Four namespace units have dependents: `monitoring-namespace` (3),
`seaweedfs-namespace` (3), `haku-namespace` (2) and the parked `gecko-namespace`
(1), all with `prune: true`. A Namespace with exactly one tenant folds into that
one Kustomization instead (rule 5). The shared ones collapse into one
`namespaces` Kustomization with `prune: false`; the `dependsOn` edges pointing at
them disappear.

This moves an existing object between Kustomizations, so it is the one batch
with a real hazard: a Namespace prune cascade-deletes its contents, and here
that contents belongs to more than one owner. Orphan each Namespace from its
current owner and verify it is unowned before the new Kustomization adopts it.
Do it in slices, not one PR.

## 4. Register or delete the remaining class-2 edges

27 edges point at a Kustomization that serves no CRD: the namespace units above
(9), `claude-rbac` (4), `haku-rbac` (3), `kyverno-policies` (3),
`haku-egress-proxy` (2), and one each at `clickhouse`, `atuin`,
`user-agentydragon`, `matrix`, `grocy-sf` and `grocy-vallejo`. Each is deleted,
or kept with a reason in `_ORDERING_EXCEPTIONS`. Keep there, too, the
CNI/SOPS/source edges without which `bazel run //cluster:bootstrap` does not
finish.

`clickhouse-schema` no longer gates `aiquota`, and still depends on `clickhouse`.
That is rule 6's first shape — **no ordering between the migration Job and the
Deployment** — and right only if `aiquota` tolerates starting against the
un-migrated schema; rule 6's third shape is the fallback if it does not. Same
trap as <../../k8s/parked/paperless/TODO.md> § "Re-establish deployment and
bootstrap ordering on revival".

## 5. Validation for the policy

In <../../validation/>:

- Extend `validate_operator_dependencies` to admission webhooks.
- `_ORDERING_EXCEPTIONS`: a table in `dependencies.py`; every `dependsOn` edge
  not derivable from rule 1 must appear in it with a reason. Land it with the
  table already populated from what survives batch 4, so it is green on arrival.
- Longest-chain cap, ratcheted down as batches land. 5 today.
- A Kustomization managing fewer than two objects must claim a rule-3 exception.
- `wait` / `healthChecks` require at least one dependent.
- A `destructive-if-out-of-order` exception must name a dependency whose
  `sourceRef` matches the dependent's — rule 6. Any other spelling documents an
  ordering the cluster does not enforce, and the check is what keeps that from
  being written down and believed.

Each check lands with or after the batch that makes it pass.

## 6. Collapse the remaining splits

17 top-level directories still hold more than one Kustomization, 74 in all, most
of them for rule-3 reasons: CRDs (`kubevirt`/`cdi`, `kyverno`, `external-secrets`,
`cert-manager`), persistent state (`seaweedfs`'s buckets and filer database,
`authentik-db-backups`), or a `Terraform` CR (`authentik-tf`, `litellm-keys-tf`).
Check each remaining unit against rule 3 and merge the ones no exception covers,
dropping an `ArtifactGenerator` entry each; `seaweedfs-secrets` is the first to
check.
