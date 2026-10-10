"""Project model authorization and fallback policies into tf/gitops/litellm-keys inputs.

A lane groups allowed routes and optional team routing fallbacks. Terraform attaches
these policies to virtual keys and teams; this module neither mints model identities
nor treats a fallback as permission to use a model.
"""

from __future__ import annotations

from cdk8s import App, Chart
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.secret_ref import SecretRef
from model_catalog.policies import KEY_MODEL_LANES, ModelLaneRoutes

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/keys-tf"
TF_MODULE = "litellm-keys"


class KeysVars(BaseModel):
    """The inputs of tf/gitops/litellm-keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_lanes: dict[str, ModelLaneRoutes] = Field(
        description="Policies keyed by client-lane identifier (for example codex_client_models), not model ID "
        "or provider. Terraform keys may combine the allowed_models from several lanes."
    )

    @field_serializer("model_lanes")
    def serialize_model_lanes(self, lanes: dict[str, ModelLaneRoutes]) -> dict[str, dict[str, list[str]]]:
        return {
            name: {
                "allowed_models": [route.id for route in lane.allowed],
                "fallback_models": [route.id for route in lane.fallbacks],
            }
            for name, lane in lanes.items()
        }


def keys_chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    """Mints the agent and laptop-client LiteLLM virtual keys (tf/gitops/litellm-keys).
    Needs the SOPS-managed master key and a serving LiteLLM with its virtual-key DB;
    tofu-controller retries on its interval until LiteLLM is up.
    """
    chart = Chart(app, TF_MODULE, disable_resource_name_hashes=True)
    terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=KeysVars(model_lanes=KEY_MODEL_LANES),
        env=[
            # The narrow SOPS age private key (litellm-clients-sops-age-key.sops.yaml
            # beside this CR) that decrypts the module's pinned client-key files for
            # its `sops_file` data sources -- single-purpose, not the broad cluster key.
            terraform.secret_env(
                "SOPS_AGE_KEY", SecretRef(namespace=terraform.NAMESPACE, name="litellm-clients-sops-age-key").key("key")
            )
        ],
    )
    return chart


# The litellm-keys Terraform CR lives DOWNSTREAM of the litellm app, not in
# litellm-secrets: minting virtual keys needs a serving LiteLLM with its
# virtual-key DB. Coupling the TF's health into litellm-secrets (the app's
# dependency) deadlocked the 2026-07-02 rollout — the app never applied the
# DATABASE_URL deployment because its secrets layer waited on a TF apply that
# needed the app. Dependency direction here is the fix.
def litellm_keys_tf(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    name = "litellm-keys-tf"
    return flux_kustomization(
        chart, name, directory, timeout="10m", depends_on=flux_kustomization_depends_on_many(tofu_controller)
    )
