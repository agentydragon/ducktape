# Attic JWT rotator

Every token has one `storage` variant. `sops` stores its JWT in the configured
SOPS file, `kubernetes` stores it in the configured Secret and notifies the
configured OpenTofu Workspace, and `mirrored` does both while consumers migrate.
The variant determines which destinations exist; mismatched or missing fields
fail config validation.

For Kubernetes-backed variants, the Secret is canonical. The rotator checks the
JWT subject and expiry claim plus pull and push scope annotations. The Secret
must be provisioned first; an empty Secret forces minting. It patches Secret
data and metadata through stdin, then reads `metadata.uid` and
`metadata.resourceVersion` and patches the Workspace annotation to
`<uid>/<resourceVersion>`. It retries this notification on every run. Secret
values do not go in process arguments or logs.

Only `sops` and `mirrored` tokens write `sops_file` and enter git commit
bookkeeping. Kubernetes-only tokens publish and notify, then return without a
SOPS write or git commit.

The Config parser temporarily tags only the previous SOPS and mirrored shapes,
which both required `sops_file`. TODO: remove that adapter after all deployed
rotators ConfigMaps carry `storage` tags. The current CI writer remains
`mirrored` until the union-capable runtime is deployed; a follow-up can then
remove its SOPS output.
