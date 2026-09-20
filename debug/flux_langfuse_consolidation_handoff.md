# Langfuse Flux ownership handoff

The protection PR changes only the deletion behavior of these six Flux
Kustomizations in `ducktape-flux`:

- `langfuse-namespace`
- `langfuse-db`
- `langfuse-cache`
- `langfuse-secrets`
- `langfuse-seaweed`
- `seaweedfs-langfuse-bucket`

They remain active until cutover. The consolidation PR folds their inventories into
`langfuse`, preserving resource names, namespaces, database/cache specifications,
bucket identity, IAM identity, credential Secret names, and encrypted secret contents.
Shared ClickHouse and the operators remain outside this ownership move.

## Cutover

1. Merge protection by itself. Verify all six **live** Kustomizations have
   `prune: false`, `deletionPolicy: Orphan`, and Ready status at their current
   generation. A merged commit alone is not evidence of reconciled protection.
2. Refresh the baseline below immediately before cutover: Namespace, CNPG Cluster,
   RedisReplication and PVC UIDs; PVC volume bindings; Bucket, S3Identity and
   S3Credentials UIDs/status; the generated credential Secret UID and owner reference
   (metadata only). Record the physical bucket's object count/size and identity/key
   continuity without logging credentials. Confirm database/cache and application
   health. Bucket writes may continue, so post-cutover counts need not be identical.
3. When consolidation CI is green and the PR is ready to merge, suspend all six old
   owners. Re-read their live suspend/prune/deletionPolicy fields immediately before
   merging. Do not suspend the surviving `langfuse` owner or shared operators.
4. Merge consolidation. Allow the new ExternalArtifact to publish and `langfuse` to
   reconcile. The old owners are removed with Orphan semantics. Do not delete any
   orphaned workload, Secret, volume, bucket or IAM identity to hurry reconciliation.
5. Verify the surviving inventory is exactly the union of the old seven inventories
   and every direct object's Flux ownership label is `langfuse`. Check unchanged
   Namespace/Cluster/RedisReplication/PVC UIDs and volume bindings, unchanged S3
   resource and credential Secret identities, and healthy database/cache, HelmRelease,
   web and worker. Verify actual S3 access and the Langfuse application path.

The consolidation also makes Helm installation/upgrades retry and removes LiteLLM's
readiness edge to `langfuse-secrets`; it does not replace that edge with a dependency on
all of Langfuse. LiteLLM's existing reflected credential references remain unchanged.

## Recovery

If adoption stalls, keep the stateful resources and investigate the surviving owner's
artifact, decryption, apply or health failure. Do not blindly revert the consolidation:
shrinking the surviving owner's inventory while it has `prune: true` could prune the
resources being moved back. Any reverse ownership transfer needs the same protection
and observed adoption procedure, starting with protection of the surviving owner.

Retain the old rollback S3 credential Secret and key. Revocation, credential rotation,
bucket cleanup, chart/image upgrades, and database/cache changes are separate work.

## Baseline observed 2026-09-20

This is a pre-PR observation, not a substitute for the fresh cutover snapshot.

| Kind/name                                                       | UID                                  | PVC volumeName                           |
| --------------------------------------------------------------- | ------------------------------------ | ---------------------------------------- |
| Namespace/langfuse                                              | ab939898-cdd1-4af7-8647-18688f719676 | —                                        |
| Cluster/langfuse-db                                             | e45a1ca0-3381-4ef2-8845-95d95b9be0db | —                                        |
| RedisReplication/langfuse-valkey-ovh                            | abb9b743-7e3f-4aed-8ee1-11bb57289f0c | —                                        |
| PersistentVolumeClaim/langfuse-db-1                             | 69b11281-9f2e-431c-94b4-95dbf2cc8898 | pvc-69b11281-9f2e-431c-94b4-95dbf2cc8898 |
| PersistentVolumeClaim/langfuse-db-3                             | b46c41f4-d6ef-47e7-88e3-27cd7b932f05 | pvc-b46c41f4-d6ef-47e7-88e3-27cd7b932f05 |
| PersistentVolumeClaim/langfuse-valkey-ovh-langfuse-valkey-ovh-0 | 8ef99547-82d9-48b6-9bbf-e250c14f716d | pvc-8ef99547-82d9-48b6-9bbf-e250c14f716d |
| PersistentVolumeClaim/langfuse-valkey-ovh-langfuse-valkey-ovh-1 | 83d6bf14-f587-4123-8002-8830a4c803a2 | pvc-83d6bf14-f587-4123-8002-8830a4c803a2 |

| Seaweed resource                               | UID                                  |
| ---------------------------------------------- | ------------------------------------ |
| Seaweed seaweedfs/seaweedfs                    | a603d7a6-93bc-4ac1-bdf1-28e39544c62f |
| Bucket langfuse/langfuse                       | 6cfa5730-5f07-49a5-9b34-87b8f0a9afd2 |
| S3Identity seaweedfs/langfuse                  | 34eecee3-ec1b-4c84-b12d-23030ca2aeee |
| S3Credentials langfuse/langfuse                | a9873372-0875-434e-944f-6e870d45628a |
| ResourceReferenceGrant seaweedfs/langfuse      | 4575b10e-5506-4bce-b4c6-7b6d20b53bc0 |
| Secret langfuse/langfuse-seaweedfs-credentials | b36206ee-2d34-4df7-8ae0-853e1d974b5a |

The credential Secret is controller-owned by the listed S3Credentials UID. The
access-key identifier recorded in S3Credentials status had SHA-256 fingerprint
`fbe2d326a83f3da1801ee53cf19adc84ab48e1c48b1ec00a80aaacb416928a01`;
no credential value is recorded here.

The Bucket reported 630,140 objects / 490,469,823,620 bytes at
`2026-09-20T22:52:42Z`. This is Seaweed operator collection accounting, not an
independent authenticated S3 listing. Bucket, identity and credentials all reported
Ready; their `reclaimPolicy: Retain` and existing specs remain unchanged.
