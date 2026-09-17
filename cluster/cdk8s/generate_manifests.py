"""Synthesize each converted directory's manifests with Python cdk8s, writing
them directly into their `cluster/k8s` directory.

Every generated Deployment/Job/CronJob carries a placeholder image tag -- each
directory's own hand-written `image-pins/kustomization.yaml` Kustomize
Component (never written by this generator) carries Flux's `$imagepolicy`
marker and overrides the real tag at `kustomize build` time. See
cluster/docs/cdk8s.md.
"""

from pathlib import Path

from cdk8s import App, Chart, Yaml
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux_constructs import NAMESPACE, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.ha_mcp_constructs import HaMcp
from cluster.cdk8s.litellm_constructs import LiteLLMProxy, LiteLLMServiceMonitor, proxy_specs
from util.bazel.workspace import get_build_workspace_directory

_LITELLM_APP_DIR = "cluster/k8s/litellm/app"
_HA_MCP_DIR = "cluster/k8s/agents/ha-mcp/app"


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


def _generate_ha_mcp(root: Path) -> None:
    name = "ha-mcp"
    app_dir = root / _HA_MCP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, name, disable_resource_name_hashes=True)
    HaMcp(chart, "ha-mcp")
    app.synth()

    _write_yaml(
        app_dir / "flux-kustomization.yaml",
        flux_kustomization(
            name,
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                timeout="5m",
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace=NAMESPACE
                ),
                path=f"./{_HA_MCP_DIR}",
                prune=True,
                wait=True,
                health_checks=[
                    KustomizationSpecHealthChecks(
                        api_version="batch/v1", kind="Job", name="ha-mcp-token-provisioner", namespace="home-assistant"
                    ),
                    KustomizationSpecHealthChecks(api_version="apps/v1", kind="Deployment", name=name, namespace=name),
                ],
                # bearer.sops.yaml (hand-written, stays alongside this generated output --
                # see cluster/docs/cdk8s.md) is SOPS-encrypted; without this Flux applies the
                # ENC[...] ciphertext literally and the facade rejects every call from haku-console.
                decryption=KustomizationSpecDecryption(
                    provider=KustomizationSpecDecryptionProvider.SOPS,
                    secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
                ),
                depends_on=[
                    KustomizationSpecDependsOn(name=dep)
                    for dep in (
                        "external-secrets-config",
                        "forgejo-images",
                        "home-assistant",
                        "monitoring-crds",  # the ServiceMonitor CRD
                    )
                ],
            ),
        ),
    )
    _write_yaml(
        app_dir / "kustomization.yaml",
        # bearer.sops.yaml stays hand-written; this generated file just lists it as a plain
        # sibling resource -- cdk8s never touches its bytes. See cluster/docs/cdk8s.md.
        kustomize_kustomization(resources=[f"{name}.k8s.yaml", "bearer.sops.yaml"], components=["./image-pins"]),
    )


def generate_manifests(root: Path) -> None:
    """Write every converted directory's generated manifests under `root`."""
    _generate_litellm_app(root)
    _generate_ha_mcp(root)


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
