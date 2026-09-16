# Flux Kustomization consolidation

Bring the existing graph to <../flux_kustomization_policy.md>. Counts are from
2026-09-16 (294 in Git, 278 applied, 713 `dependsOn` edges, longest chain 14).

Batches are independently landable and mostly independently reviewable; the
ordering below is by risk, not by dependency. Anything touching a CNPG
`Cluster`, PVC, `Bucket`, `S3Identity` or `Terraform` CR follows <../../AGENTS.md>
§ Migrating stateful Flux Kustomizations — suspend, `prune: false`,
`deletionPolicy: Orphan`, verify identity, then cut over.

## 1. Drop the `external-secrets-config` edges

52 edges. An `ExternalSecret` naming a `ClusterSecretStore` that does not exist
yet stays `SecretSyncedError` and syncs when it appears — class 2.

The sibling edges to `external-secrets-operator` **stay**: ESO registers
`failurePolicy: Fail` webhooks on `externalsecrets` and `secretstores`, so the
apply really is rejected while the operator is down. `external-secrets-crds`
alone does not satisfy those 84 consumers.

## 2. Drop ungated `wait` / `healthChecks`

66 unsuspended Kustomizations set `wait: true` or `healthChecks` with nothing
depending on them. Pure deletion, no resource movement. Three were parked in a
5–10 minute timeout at the time of measurement, out of eight worker slots.

## 3. Delete class-2 hub edges

Largest first; each target is one PR.

| Edges | Target                   | Why it is class 2                                                      |
| ----- | ------------------------ | ---------------------------------------------------------------------- |
| 48    | `forgejo-images`         | A pull credential. Absent, the pod is `ImagePullBackOff` and recovers. |
| 42    | `gateway`                | An unrouted `HTTPRoute` binds when the Gateway appears.                |
| 30    | `local-path-provisioner` | A PVC with no StorageClass stays `Pending` and binds later.            |
| 24    | `reflector`              | Same shape as a pull credential.                                       |
| 22    | `seaweedfs-cluster`      | A `Bucket` CR waits for its operator; the filer being up is runtime.   |
| 19    | `tofu-state-db`          | Only `Terraform` CRs need it, and they retry.                          |
| 14    | `authentik`              | OIDC clients retry the discovery endpoint.                             |
| 12    | `external-creds`         | Credential distribution.                                               |

Keep, as registered rule-2 exceptions, the CNI/SOPS/source edges without which
`bazel run //cluster:bootstrap` does not finish. Register them in
`_ORDERING_EXCEPTIONS` in the same PR that adds the check (batch 6).

`clickhouse-schema` before `aiquota` is the one genuine
destructive-if-out-of-order pair here, and the edge alone does not deliver it:
different artifacts means `Ready`-gating, so on a commit that changes both,
`aiquota` can apply against the old schema. Rebuild it as a rule-6 shared
artifact carrying both paths. Until that lands the edge stays, but as a
registered exception documenting an intent that is not yet implemented — which
is also a live correctness bug, not only a cleanup.

## 4. Split CRDs out of the webhookless operators

Lower priority than its size suggests: the depth-3 figure in the policy document
is reached with class-1 edges pointing at _operators_, so this batch buys
resilience (a sick operator stops blocking its consumers' applies), not chain
depth.

Eligible — no admission webhook over their CRs, confirmed 2026-09-16:
`seaweedfs-operator` (58 consumers), `monitoring-stack` (22),
`tofu-controller` (19), `valkey` (10). Each is Helm-installed, so each split is
real work: `crds.enabled=false` or the chart's equivalent, plus a
`<operator>-crds` Kustomization sourcing them. Take them in order and stop where
the consumer count stops paying for it.

Not eligible: ESO, `cnpg`, `cert-manager`, `kyverno`, `kubevirt`/`virt-api`,
`cdi` all register `failurePolicy: Fail` webhooks. Their consumers depend on the
operator, and that edge is class 1. Re-check with
`kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations`
before moving one; charts add and remove webhooks across versions.

## 5. Central `namespaces` Kustomization

~35 Namespace-only Kustomizations collapse into one with `prune: false`, and the
~60 `dependsOn` edges pointing at them disappear.

This moves an existing object between Kustomizations, so it is the one batch
with a real hazard: a Namespace prune cascade-deletes its contents. Orphan each
Namespace from its current owner and verify it is unowned before the new
Kustomization adopts it. Do it in slices, not one PR.

## 6. Replace the validation law

In <../../validation/>:

- Delete `check_crd_layering` and `MIXED_CRD_LAYERING_EXCEPTIONS`
  (`crd_layering.py`), and `test_crd_layering.py`. `OPERATOR_CRDS` /
  `CRD_TO_OPERATOR` stay — `validate_operator_dependencies` is rule 1 and is the
  part that was load-bearing.
- Extend `validate_operator_dependencies` to admission webhooks, and let the
  CRD-providing Kustomization satisfy the edge where one exists.
- `_ORDERING_EXCEPTIONS`: every `dependsOn` edge not derivable from rule 1 must
  appear with a reason. Land it with the exception list already populated from
  what survives batch 3, so it is green on arrival.
- Longest-chain cap, ratcheted down as batches land. 14 today; 3 with class-1
  edges alone, so the floor is around 4–5 once exceptions are registered.
- A Kustomization managing fewer than two objects must claim a rule-3 exception.
- `wait` / `healthChecks` require at least one dependent.
- A `destructive-if-out-of-order` exception must name a dependency whose
  `sourceRef` matches the dependent's — rule 6. Any other spelling documents an
  ordering the cluster does not enforce, and the check is what keeps that from
  being written down and believed.

The deletion lands first — it blocks batches 3 and 5. The new checks land with
or after the batch that makes them pass.

## 7. Collapse role directories

~92 Kustomizations sit in `{namespace,secrets,app,db,cache,agent-rbac,servicemonitor}`
splits of a single component. `db/` and `cache/` mostly stay under rule 3b;
`namespace/` goes in batch 5; the rest merge into the component's one
Kustomization, dropping an `ArtifactGenerator` entry and a root-kustomization
line each.

Biggest first: `forgejo` (6), `langfuse` (6), `authentik` (5), `litellm` (5),
`paperless` (5), `seaweedfs` (5), `atuin` (4), `cert-manager` (4), `gatus` (4),
`inventree` (4), `matrix` (4), `ollama` (4), `study-casino` (4).

`monitoring/{loki,mimir,tempo}` and `forgejo/app` are already consolidated under
the old `MIXED_CRD_LAYERING_EXCEPTIONS` and need nothing but the exception entry
removed in batch 6.
