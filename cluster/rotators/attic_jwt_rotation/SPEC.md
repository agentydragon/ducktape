# Attic JWT rotator

Every token has common identity and scope fields plus one `destination` variant.
`destination.storage` is `sops` or `kubernetes`: SOPS stores its JWT in the
configured file; Kubernetes stores it in the configured Secret and notifies the
configured OpenTofu Workspace. The destination determines which fields exist;
mismatched or missing fields fail config validation. Before publishing a minted
JWT to either destination, the rotator checks its subject against the configured
subject and reads its expiry.

For `kubernetes` tokens, the Secret is canonical. The rotator checks the
JWT subject and expiry claim plus pull and push scope annotations. The Secret
must be provisioned first; an empty Secret forces minting. Its Flux SSA policy
is `Merge`, preserving data and annotations written by the rotator. It patches Secret
data and metadata through stdin, then reads `metadata.uid` and
`metadata.resourceVersion` and patches the Workspace annotation to
`<uid>/<resourceVersion>`. It retries this notification on every run. Secret
values do not go in process arguments or logs.

Only `sops` tokens write `sops_file` and enter git commit bookkeeping.
`kubernetes` tokens publish and notify, then return without a SOPS write or git
commit.

Generated configurations contain nested destinations with their default storage
tags included. The Ducktape CI writer and reader and the gaffer-private CI writer
use `kubernetes` storage; every other token (the host and service readers)
remains SOPS-backed.
