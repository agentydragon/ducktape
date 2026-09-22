"""Flux Kustomizations for the cluster/k8s/litellm slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


# The litellm-keys Terraform CR lives DOWNSTREAM of the litellm app, not in
# litellm-secrets: minting virtual keys needs a serving LiteLLM with its
# virtual-key DB. Coupling the TF's health into litellm-secrets (the app's
# dependency) deadlocked the 2026-07-02 rollout — the app never applied the
# DATABASE_URL deployment because its secrets layer waited on a TF apply that
# needed the app. Dependency direction here is the fix.
def litellm_keys_tf(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    litellm: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
) -> Kustomization:
    name = "litellm-keys-tf"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            # Decrypt litellm-clients-sops-age-key.sops.yaml (the narrow SOPS_AGE_KEY for
            # the tf-runner) so sops_file in tf/gitops/litellm-keys can read the virtual-key
            # SSOT. Added when that SOPS file arrived — previously this dir held only plain YAML.
            decryption=SOPS_DECRYPTION,
            source_ref=artifact_source_ref(artifact),
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="litellm-keys",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # The app must serve (with its DB) before keys can mint.
                litellm,
                tofu_controller,
                tofu_state_db,
            ),
        ),
    )
