# Attic JWT rotator

Every token has common identity and scope fields plus one `destination` variant.
`destination.storage` is `sops`, `kubernetes`, or `mirrored`: SOPS stores its JWT
in the configured file, Kubernetes stores it in the configured Secret and
notifies the configured OpenTofu Workspace, and mirrored does both. The
destination determines which fields exist; mismatched or missing fields fail
config validation.

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

Generated configurations contain nested destinations with their default storage
tags included. The parser still accepts the previous flat shape during rollout.
Remove that adapter after every deployed rotator ConfigMap uses nested
destinations. The Ducktape CI writer uses Kubernetes-only storage; per-host
readers and the gaffer-private writer remain SOPS-backed.
