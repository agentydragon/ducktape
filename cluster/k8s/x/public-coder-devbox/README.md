# Retired public-coder devbox

The public-coder-agent KubeVirt devbox was retired to reclaim its reserved
worker memory and OVH local-path disk. This directory is archival only: it is
not referenced by `cluster/k8s/kustomization.yaml` and has no Flux
Kustomization. Do not apply these files without restoring the supporting
secrets, policies, and agent wiring deliberately.

The active VM resources were removed from the old Flux-managed path so the
`prune: true` controller can delete them after this change is reconciled.
