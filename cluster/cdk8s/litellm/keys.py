"""Apply the BuildBuddy credential used by the LiteLLM live-test key sync."""

from __future__ import annotations

from cdk8s import Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/keys-tf"


def litellm_keys_tf(chart: Chart, directory: RenderedDirectory, litellm: Kustomization) -> Kustomization:
    """Apply the credential sibling after the LiteLLM application is ready."""
    return flux_kustomization(
        chart,
        "litellm-keys-tf",
        directory,
        timeout="20m",
        depends_on=flux_kustomization_depends_on_many(litellm),
        description="Provide the BuildBuddy org-admin API key used by LiteLLM's live-test sync.",
    )
