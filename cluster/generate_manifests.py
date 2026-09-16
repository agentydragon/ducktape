"""Synthesize the LiteLLM manifests with Python cdk8s, writing them directly into
`cluster/k8s/litellm/app`.

The Deployment is fully generated, with a placeholder image tag --
`image-pins/kustomization.yaml` (hand-written Kustomize Component, never
written by this generator) carries Flux's `$imagepolicy` marker and overrides
the real tag at `kustomize build` time. See cluster/docs/plans/cdk8s_adoption.md.
"""

from pathlib import Path

from cdk8s import App, Chart, Yaml

from cluster.flux_constructs import FluxDependency, FluxKustomizationSpec, flux_kustomization, kustomize_kustomization
from cluster.litellm_constructs import LiteLLMProxy, LiteLLMServiceMonitor, proxy_specs
from util.bazel.workspace import get_build_workspace_directory

_APP_DIR = "cluster/k8s/litellm/app"


def _write_yaml(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(Yaml.format_objects([manifest]))


def generate_manifests(root: Path) -> None:
    """Write the generated litellm/app manifests under `root`."""
    (spec,) = proxy_specs()  # only one LiteLLM proxy today; extend proxy_specs() when a second lands

    app_dir = root / _APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    LiteLLMProxy(Chart(app, spec.name, disable_resource_name_hashes=True), "proxy", spec)
    LiteLLMServiceMonitor(Chart(app, "litellm-servicemonitor", disable_resource_name_hashes=True), "monitoring")
    app.synth()

    _write_yaml(
        app_dir / "flux-kustomization.yaml",
        flux_kustomization(
            FluxKustomizationSpec(
                name="litellm",
                path=f"./{_APP_DIR}",
                interval="10m",
                timeout="10m",
                depends_on=(
                    FluxDependency("external-secrets-config"),
                    FluxDependency("forgejo-images"),
                    FluxDependency("litellm-secrets"),
                    FluxDependency("litellm-db"),
                    FluxDependency("gateway"),
                    FluxDependency("cert-manager-environment"),
                    FluxDependency("langfuse-secrets"),
                    FluxDependency("reflector"),
                    FluxDependency("tana-mcp"),
                    # The ServiceMonitor/PodMonitor CRD (folded in from the retired
                    # litellm-servicemonitor Kustomization, #7103).
                    FluxDependency("monitoring-crds"),
                ),
            )
        ),
    )
    _write_yaml(
        app_dir / "kustomization.yaml",
        kustomize_kustomization(
            namespace="litellm",
            resources=[f"{spec.name}.k8s.yaml", "litellm-servicemonitor.k8s.yaml"],
            components=["./image-pins"],
        ),
    )


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
