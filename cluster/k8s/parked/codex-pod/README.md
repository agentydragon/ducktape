# Parked codex-pod credentials

The retired Codex pod's SSH bootstrap identity and Forgejo `tea` token remain here
as SOPS-encrypted Secret manifests. They are deliberately excluded from the
source-owned deployment Kustomization at
[`x/codex_pod_image/deploy/`](../../../../x/codex_pod_image/deploy/).

**TODO before reactivation:** replace these SOPS files with runtime-managed
credentials. They probably should not be stored as SOPS-encrypted manifests. See
the [deployment notes](../../../../x/codex_pod_image/deploy/README.md#follow-ups).
