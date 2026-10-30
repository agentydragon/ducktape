# Attic JWT rotator

`rotators.yaml` may give a token a Kubernetes destination containing a pre-existing
Secret and an OpenTofu Workspace notification. The Secret is the canonical token
state: its JWT expiry claim and the rotator's scope annotations decide whether to
mint. The Secret must exist before the rotator runs; an empty Secret is valid and
forces the first mint.

The rotator patches the JWT and expiry, pull, and push annotations through stdin,
then reads the Secret's `metadata.uid` and `metadata.resourceVersion`. It patches
the Workspace notification annotation to `<uid>/<resourceVersion>` before writing
those identifiers with the same JWT to the configured `sops_file`. The notification
is sent on every run so a failed patch is retried even when the Secret remains
fresh. Secret values never go in process arguments or logs.

The `sops_file` output remains required during provisioning because current CI
consumers still read it. Its UID and resourceVersion stamps let a later run repair
it from the canonical Secret after a failed git push. SOPS publication is a
transition output and can be removed only after those consumers migrate.
