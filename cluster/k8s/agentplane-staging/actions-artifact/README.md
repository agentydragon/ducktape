# Staging Actions artifact

This generator packages the staging Actions manifests. The existing Actions
Kustomization consumes the resulting ExternalArtifact, retaining resource ownership,
decryption, dependency gates, and health checks.

The output revision hashes content; `originRevision` records Git provenance without
turning unrelated commits into artifact updates. Keep the repository-relative path
so existing manifest validation and the eventual consumer path remain unchanged.
SOPS content stays encrypted until the consuming Kustomization decrypts it.

The generator is installed separately from the root-owned source-watcher CRD and
controller. Source-watcher v2.1.1 comes from the Flux v2.8.8 release's
`manifests.tar.gz`, matching the existing controller manifest release. Its resources
are appended to `flux-system/gotk-components.yaml` so raw Terraform bootstrap also
installs them; the existing shared Flux RBAC already includes source-watcher.

Before merging the consumer cutover, verify the preceding canary deployment:

1. Source-watcher is available and the ArtifactGenerator is Ready.
2. The ExternalArtifact has a content-derived revision and Git origin revision.
3. An unrelated Git commit leaves the artifact revision unchanged; an Actions
   input edit changes it. Periodic drift reconciles are still expected.
4. Building the packaged Actions directory yields the same resource identities
   and content as building directly from Git.

The local contract test checks complete render inputs, not live digest behavior.
Do not claim the canary passed until the controller has produced the artifact.
Image automation continues editing the original Git manifests.

After cutover, verify Actions' applied revision matches the ExternalArtifact, both
replicas and Service endpoints are ready, and an authenticated Actions request
succeeds through staging. Shared dependencies can still block while they reconcile;
this pilot does not remove their gates or change the 30-second dependency retry.

To roll back, restore the Actions sourceRef to GitRepository `ducktape` in namespace
`ducktape-flux`. Keep the same path and Kustomization name; do not delete its managed
resources or the generator during rollback.
