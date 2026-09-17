"""Synthesize each converted directory's manifests with Python cdk8s, writing
them directly into their `cluster/k8s` directory.

The Deployment is fully generated, with a placeholder image tag --
`image-pins/kustomization.yaml` (hand-written Kustomize Component, never
written by this generator) carries Flux's `$imagepolicy` marker and overrides
the real tag at `kustomize build` time. See cluster/docs/cdk8s.md.
"""

from pathlib import Path

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, Yaml
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux_constructs import NAMESPACE, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.litellm_constructs import LiteLLMProxy, LiteLLMServiceMonitor, proxy_specs
from util.bazel.workspace import get_build_workspace_directory

_LITELLM_APP_DIR = "cluster/k8s/litellm/app"
_HA_MCP_NAMESPACE_DIR = "cluster/k8s/agents/ha-mcp/namespace"


def _write_yaml(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(Yaml.format_objects([manifest]))


def _generate_litellm_app(root: Path) -> None:
    (spec,) = proxy_specs()  # only one LiteLLM proxy today; extend proxy_specs() when a second lands

    app_dir = root / _LITELLM_APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, spec.name, disable_resource_name_hashes=True)
    LiteLLMProxy(chart, "proxy", spec)
    LiteLLMServiceMonitor(chart, "monitoring")
    app.synth()

    _write_yaml(
        app_dir / "flux-kustomization.yaml",
        flux_kustomization(
            "litellm",
            spec=KustomizationSpec(
                interval="10m",
                path=f"./{_LITELLM_APP_DIR}",
                prune=True,
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="litellm", namespace=NAMESPACE
                ),
                timeout="10m",
                depends_on=[
                    KustomizationSpecDependsOn(name=dep)
                    for dep in (
                        "external-secrets-config",
                        "forgejo-images",
                        "litellm-secrets",
                        "litellm-db",
                        "gateway",
                        "cert-manager-environment",
                        "langfuse-secrets",
                        "reflector",
                        "tana-mcp",
                        # The ServiceMonitor/PodMonitor CRD (folded in from the retired
                        # litellm-servicemonitor Kustomization, #7103).
                        "monitoring-crds",
                    )
                ],
            ),
        ),
    )
    _write_yaml(
        app_dir / "kustomization.yaml",
        kustomize_kustomization(namespace="litellm", resources=[f"{spec.name}.k8s.yaml"], components=["./image-pins"]),
    )


def _generate_ha_mcp_namespace(root: Path) -> None:
    name = "ha-mcp-namespace"
    app_dir = root / _HA_MCP_NAMESPACE_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, name, disable_resource_name_hashes=True)
    ApiObject(
        chart,
        "namespace",
        api_version="v1",
        kind="Namespace",
        metadata=ApiObjectMetadata(
            name="ha-mcp", labels={"app.kubernetes.io/name": "ha-mcp", "goldilocks.fairwinds.com/enabled": "false"}
        ),
    )
    app.synth()

    _write_yaml(
        app_dir / "flux-kustomization.yaml",
        flux_kustomization(
            name,
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                path=f"./{_HA_MCP_NAMESPACE_DIR}",
                prune=True,
                wait=True,
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace=NAMESPACE
                ),
            ),
        ),
    )
    _write_yaml(app_dir / "kustomization.yaml", kustomize_kustomization(resources=[f"{name}.k8s.yaml"]))


def generate_manifests(root: Path) -> None:
    """Write every converted directory's generated manifests under `root`."""
    _generate_litellm_app(root)
    _generate_ha_mcp_namespace(root)


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
