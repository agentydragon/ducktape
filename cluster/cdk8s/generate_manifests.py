"""Synthesize each converted directory's manifests with Python cdk8s, writing
them directly into their `cluster/k8s` directory.

Every generated Deployment/Job/CronJob carries a placeholder image tag -- each
environment's own hand-written `image-pins/kustomization.yaml` Kustomize
Component (never written by this generator) carries Flux's `$imagepolicy`
marker and overrides the real tag at `kustomize build` time. See
cluster/docs/cdk8s.md.
"""

from collections.abc import Callable
from pathlib import Path
from typing import cast

from cdk8s import ApiObject, App, Chart, Yaml
from cdk8s_plus_34 import ConfigMap
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s import haku_openclaw_spike_config, public_coder_agent_config
from cluster.cdk8s.agentplane import staging, testing
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import json5_config
from cluster.cdk8s.flux_constructs import NAMESPACE, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.ha_mcp_constructs import HaMcp
from cluster.cdk8s.litellm_constructs import LiteLLMProxy, LiteLLMServiceMonitor, proxy_specs
from cluster.cdk8s.metadata import metadata
from util.bazel.workspace import get_build_workspace_directory

_LITELLM_APP_DIR = "cluster/k8s/litellm/app"
_HA_MCP_DIR = "cluster/k8s/agents/ha-mcp/app"

# The chart objects whose readiness gates the environment, in the order the checks are
# listed. The trust-manager Bundle writes its target ConfigMap asynchronously, outside
# the rendered input, so that ConfigMap is checked explicitly rather than via `wait`.
_HEALTH_CHECK_KINDS = ("Namespace", "Cluster", "Database", "Deployment", "Certificate", "Bundle")
_CNPG_DATABASE_READY = (
    "has(status.applied) && status.applied && "
    "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
)
_HAKU_OPENCLAW_SPIKE_APP_DIR = "cluster/k8s/agents/haku-openclaw-spike/app"
_PUBLIC_CODER_AGENT_APP_DIR = "cluster/k8s/agents/public-coder-agent/app"


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


def _agentplane_health_checks(chart: Chart, namespace: str) -> list[KustomizationSpecHealthChecks]:
    # `Chart.api_objects` is direct children only; every object here sits inside a Construct.
    api_objects = [cast(ApiObject, node) for node in chart.node.find_all() if ApiObject.is_api_object(node)]
    objects = sorted(
        (obj for obj in api_objects if obj.kind in _HEALTH_CHECK_KINDS),
        key=lambda obj: _HEALTH_CHECK_KINDS.index(obj.kind),
    )
    checks = [
        KustomizationSpecHealthChecks(
            api_version=obj.api_version, kind=obj.kind, name=obj.name, namespace=obj.metadata.namespace
        )
        for obj in objects
    ]
    # trust-manager names a Bundle's target ConfigMap after the Bundle.
    checks.extend(
        KustomizationSpecHealthChecks(api_version="v1", kind="ConfigMap", name=obj.name, namespace=namespace)
        for obj in objects
        if obj.kind == "Bundle"
    )
    return checks


def _generate_agentplane(root: Path, env: Environment) -> None:
    """Synthesize the environment's chart into `cluster/k8s/<namespace>` as a single
    `agentplane.k8s.yaml`. Single failure domain by design -- including the CNPG Postgres
    `Cluster` -- accepted for both non-production environments.

    Also (re)writes the directory's Flux Kustomization (health checks derived from the
    chart's own objects) and its root Kustomization: the one generated file plus the
    environment's hand-written `extra_resources`. The sibling image-pins/ Component stays
    hand-written, same as litellm/ha-mcp.
    """
    env_dir = f"cluster/k8s/{env.namespace}"
    out_dir = root / env_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = environment_chart(app, env)
    app.synth()

    _write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            env.namespace,
            description=env.flux_description,
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                timeout="10m",
                path=f"./{env_dir}",
                prune=True,
                # This one Kustomization owns the CNPG Cluster's PVCs; pruning on
                # deletion would take the database with them.
                deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
                health_checks=_agentplane_health_checks(chart, env.namespace),
                health_check_exprs=[
                    KustomizationSpecHealthCheckExprs(
                        api_version="postgresql.cnpg.io/v1", kind="Database", current=_CNPG_DATABASE_READY
                    )
                ],
                decryption=(
                    KustomizationSpecDecryption(
                        provider=KustomizationSpecDecryptionProvider.SOPS,
                        secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
                    )
                    if any(resource.endswith(".sops.yaml") for resource in env.extra_resources)
                    else None
                ),
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=env.namespace, namespace=NAMESPACE
                ),
                depends_on=[KustomizationSpecDependsOn(name=dep) for dep in env.depends_on],
            ),
        ),
    )
    _write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(resources=["agentplane.k8s.yaml", *env.extra_resources], components=["./image-pins"]),
    )


def _build_config_map_chart(
    app: App, *, chart_name: str, configmap_name: str, namespace: str, data: dict[str, str]
) -> Chart:
    """Build a single-ConfigMap chart without synthesizing it -- shared by
    `_write_config_map_chart` (writes it to disk) and tests (in-memory synth)."""
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=metadata(configmap_name, namespace), data=data)
    return chart


def _write_config_map_chart(root: Path, app_dir: str, chart_builder: Callable[[App], Chart]) -> None:
    """Synthesize `chart_builder`'s output into `app_dir`, whose `flux-kustomization.yaml`
    and `kustomization.yaml` stay hand-written (cluster/docs/cdk8s.md § SOPS secrets in
    a converted directory).
    """
    out_dir = root / app_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart_builder(app)
    app.synth()


def _haku_openclaw_spike_config_chart(app: App) -> Chart:
    return _build_config_map_chart(
        app,
        chart_name="haku-openclaw-spike-config",
        configmap_name="haku-openclaw-spike-config",
        namespace="haku-openclaw-spike",
        data={
            "openclaw.json": json5_config(haku_openclaw_spike_config.config()),
            "claude.json": json5_config(haku_openclaw_spike_config.claude_config()),
        },
    )


def _generate_haku_openclaw_spike_config(root: Path) -> None:
    _write_config_map_chart(root, _HAKU_OPENCLAW_SPIKE_APP_DIR, _haku_openclaw_spike_config_chart)


def _public_coder_agent_config_chart(app: App) -> Chart:
    return _build_config_map_chart(
        app,
        chart_name="public-coder-agent-config",
        configmap_name="public-coder-agent-config",
        namespace="public-coder-agent",
        data={"openclaw.json5": json5_config(public_coder_agent_config.config())},
    )


def _generate_public_coder_agent_config(root: Path) -> None:
    _write_config_map_chart(root, _PUBLIC_CODER_AGENT_APP_DIR, _public_coder_agent_config_chart)


def generate_manifests(root: Path) -> None:
    """Write every converted directory's generated manifests under `root`."""
    _generate_litellm_app(root)
    _generate_ha_mcp(root)
    for env in (staging.ENV, testing.ENV):
        _generate_agentplane(root, env)
    _generate_haku_openclaw_spike_config(root)
    _generate_public_coder_agent_config(root)


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
