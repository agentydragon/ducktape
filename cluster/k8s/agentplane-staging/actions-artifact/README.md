# Staging Actions artifact

This canary packages the staging Actions manifests without applying them. The
existing Actions Kustomization still consumes Git until the separate cutover.

The output revision hashes content; `originRevision` records Git provenance without
turning unrelated commits into artifact updates. Keep the repository-relative path
so existing manifest validation and the eventual consumer path remain unchanged.
SOPS content stays encrypted until the consuming Kustomization decrypts it.

The generator is installed separately from the root-owned source-watcher CRD and
controller. Source-watcher v2.1.1 comes from the Flux v2.8.8 release's
`manifests.tar.gz`, matching the existing controller manifest release. Its resources
are appended to `flux-system/gotk-components.yaml` so raw Terraform bootstrap also
installs them; the existing shared Flux RBAC already includes source-watcher.

Before consumer cutover, verify:

1. Source-watcher is available and the ArtifactGenerator is Ready.
2. The ExternalArtifact has a content-derived revision and Git origin revision.
3. An unrelated Git commit leaves the artifact revision unchanged; an Actions
   input edit changes it. Periodic drift reconciles are still expected.
4. Building the packaged Actions directory yields the same resource identities
   and content as building directly from Git.

The local contract test checks complete render inputs, not live digest behavior.
Do not claim the canary passed until the controller has produced the artifact.
Image automation continues editing the original Git manifests.
