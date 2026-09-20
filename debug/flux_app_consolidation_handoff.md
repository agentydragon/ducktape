# Atuin, Study Casino, and Haku Mailbox ownership handoff

Merge the ownership-protection PR and verify the live settings below before merging
any of the application consolidation PRs. The protection PR changes no workload,
database, namespace, or storage manifests. Each consolidation preserves resource
identities and moves their Flux ownership.

## Before each application cutover

1. Verify its old `<app>-namespace` and `<app>-db` Kustomizations both have
   `spec.prune: false` and `spec.deletionPolicy: Orphan`. Check the **live objects**;
   a merged Git commit alone does not establish protection.
2. Capture the current Namespace, CNPG Cluster and PVC UIDs and PVC `volumeName`s.
   Confirm the database is healthy and PVCs are Bound. Refresh the baseline if CNPG
   has legitimately replaced an instance since the snapshot below.
3. Suspend the two old owners immediately before merging that application's PR.
   Verify their suspend and deletion-protection fields. Suspension alone does not
   prevent pruning when a Kustomization is deleted.
4. Merge and reconcile the application consolidation. Its surviving app
   Kustomization adopts the existing resources using their unchanged names and
   namespaces. The old owners are deleted with `Orphan` semantics.
5. Check the Namespace and Cluster UIDs, PVC UIDs and volume bindings against the
   pre-cutover snapshot, the surviving Flux inventory/ownership labels, database
   health and application readiness. Unexpected identity changes stop further
   cutovers; do not delete or recreate database/storage objects to make Flux green.

The three app PRs share the protection prerequisite and can be reviewed independently.
Do not squash protection and consolidation into one reconciliation. The separate
readiness-validator cleanup has no ownership handoff prerequisite.

## Observed database/storage baseline

Read from the live cluster on 2026-09-20 before publishing the stack. This is evidence
for comparison, not an instruction to recreate these objects.

| Namespace    | Kind/name               | UID                                  | PVC volumeName                           |
| ------------ | ----------------------- | ------------------------------------ | ---------------------------------------- |
| atuin        | Cluster/atuin-db        | 586fd60f-8fb5-48e9-9ef9-9c4abb01cc55 | —                                        |
| atuin        | PVC/atuin-db-3          | 429ff34b-3d3e-4ff9-95ce-8d06a747e9fc | pvc-429ff34b-3d3e-4ff9-95ce-8d06a747e9fc |
| atuin        | PVC/atuin-db-4          | 0442a585-0ead-430a-9191-b724e0eab07f | pvc-0442a585-0ead-430a-9191-b724e0eab07f |
| study-casino | Cluster/study-casino-db | 0816ea8f-e9c5-4489-b4f4-6227459dfcd3 | —                                        |
| study-casino | PVC/study-casino-db-13  | c7a354c2-8738-4fa1-a730-e4e8bb6b66d1 | pvc-c7a354c2-8738-4fa1-a730-e4e8bb6b66d1 |
| study-casino | PVC/study-casino-db-14  | 5cc630e4-4ff5-4d9f-a16b-b47df5305d1a | pvc-5cc630e4-4ff5-4d9f-a16b-b47df5305d1a |
| study-casino | PVC/study-casino-db-6   | 59773290-f75e-4fb7-9033-ef517160066d | pvc-59773290-f75e-4fb7-9033-ef517160066d |
| haku-mailbox | Cluster/haku-mailbox-db | 314b0ef7-66d1-4693-80bb-12fb9db6769b | —                                        |
| haku-mailbox | PVC/haku-mailbox-db-2   | fe099480-c15d-437a-9708-a60931528a70 | pvc-fe099480-c15d-437a-9708-a60931528a70 |
| haku-mailbox | PVC/haku-mailbox-db-3   | 459abeae-c5eb-4295-9703-34daf3c1ecbb | pvc-459abeae-c5eb-4295-9703-34daf3c1ecbb |
