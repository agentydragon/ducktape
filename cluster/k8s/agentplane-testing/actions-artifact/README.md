# Testing Actions artifact

This generator packages the testing Actions manifests. The existing Actions
Kustomization consumes the resulting ExternalArtifact, retaining resource
ownership, dependency gates, and health checks.

The output revision hashes content; `originRevision` records Git provenance without
turning unrelated commits into artifact updates. The artifact includes the complete
testing Actions Kustomize input closure, including the `settings.yaml` consumed by
its `configMapGenerator`.

The generator excludes the Flux Kustomization declaration itself because that
control object remains managed from the repository by the root Kustomization.
To roll back, restore the consumer's sourceRef to GitRepository `ducktape` while
keeping the same Kustomization name and path.
