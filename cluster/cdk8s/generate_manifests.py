"""Synthesize each converted directory's manifests with Python cdk8s, writing
them directly into their `cluster/k8s` directory, plus the mesh roster's projection
for `tf/gitops/dns-records`.

Every generated Deployment/Job/CronJob carries a placeholder image tag -- each
environment's own hand-written `image-pins/kustomization.yaml` Kustomize
Component (never written by this generator) carries Flux's `$imagepolicy`
marker and overrides the real tag at `kustomize build` time. See
cluster/docs/cdk8s.md.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from cdk8s import App, Chart, Yaml
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

from cluster.cdk8s import (
    aiquota_constructs,
    clickhouse_schema_constructs,
    descheduler_constructs,
    egress_fences,
    etcd_constructs,
    haku_openclaw_spike_config,
    public_coder_agent_config,
    stateful_infra,
    terraform_constructs,
)
from cluster.cdk8s.agentplane import staging, testing
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import json5_config
from cluster.cdk8s.etcd_constructs import TalosEtcdMetrics
from cluster.cdk8s.flux_constructs import NAMESPACE, flux_kustomization, health_checks, kustomize_kustomization
from cluster.cdk8s.ha_mcp_constructs import HaMcp
from cluster.cdk8s.haku import charts as haku_charts
from cluster.cdk8s.litellm_constructs import LiteLLMProxy, LiteLLMServiceMonitor, proxy_specs
from cluster.cdk8s.litellm_keys import model_allowlists
from cluster.cdk8s.metadata import metadata
from cluster.scripts import nebula_mesh
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory

_LITELLM_APP_DIR = "cluster/k8s/litellm/app"
_LITELLM_KEYS_TF_DIR = "cluster/k8s/litellm/keys-tf"
_HA_MCP_DIR = "cluster/k8s/agents/ha-mcp/app"
_CLICKHOUSE_SCHEMA_DIR = "cluster/k8s/clickhouse/schema"
_AIQUOTA_DIR = "cluster/k8s/aiquota"
_DNS_AUTOMATION_DIR = "cluster/k8s/dns-automation"
_ETCD_MONITORING_DIR = "cluster/k8s/monitoring/etcd"
_DNS_RECORDS_DIR = "tf/gitops/dns-records"

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
_DESCHEDULER_DIR = "cluster/k8s/descheduler"
_SEAWEEDFS_CLUSTER_DIR = "cluster/k8s/seaweedfs/cluster"
_HAKU_EGRESS_PROXY_DIR = "cluster/k8s/agents/haku-egress-proxy"
_MITMPROXY_DIR = "cluster/k8s/agents/mitmproxy"


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


def _litellm_keys_chart(app: App) -> Chart:
    """Mints the agent and laptop-client LiteLLM virtual keys (tf/gitops/litellm-keys).
    Needs the SOPS-managed master key and a serving LiteLLM with its virtual-key DB;
    tofu-controller retries on its interval until LiteLLM is up.
    """
    chart = Chart(app, "litellm-keys", disable_resource_name_hashes=True)
    terraform_constructs.gitops_terraform(
        chart,
        "terraform",
        name="litellm-keys",
        variables={"model_allowlists": model_allowlists()},
        env=[
            # The narrow SOPS age private key (litellm-clients-sops-age-key.sops.yaml
            # beside this CR) that decrypts the module's pinned client-key files for
            # its `sops_file` data sources -- single-purpose, not the broad cluster key.
            terraform_constructs.secret_env("SOPS_AGE_KEY", "litellm-clients-sops-age-key", "key")
        ],
    )
    return chart


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


def _generate_clickhouse_schema(root: Path) -> None:
    name = clickhouse_schema_constructs.NAME
    out_dir = root / _CLICKHOUSE_SCHEMA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = clickhouse_schema_constructs.chart(app)
    app.synth()

    _write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            name,
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                timeout="20m",
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace=NAMESPACE
                ),
                path=f"./{_CLICKHOUSE_SCHEMA_DIR}",
                prune=True,
                wait=True,
                health_checks=health_checks(chart, ("Job",)),
                depends_on=[KustomizationSpecDependsOn(name="clickhouse")],
            ),
        ),
    )
    _write_yaml(
        out_dir / "kustomization.yaml",
        # schema.sql stays hand-written; the generator entry renders it into the ConfigMap the
        # Job mounts. See cluster/docs/cdk8s.md.
        kustomize_kustomization(
            resources=[f"{name}.k8s.yaml"], config_map_generator=[clickhouse_schema_constructs.SCHEMA_CONFIG_MAP]
        ),
    )


def _generate_aiquota(root: Path) -> None:
    name = aiquota_constructs.NAME
    out_dir = root / _AIQUOTA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    aiquota_constructs.chart(app)
    app.synth()

    _write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            name,
            description="aiquota API with Claude and Codex quota through the CLIProxyAPI integration.",
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                timeout="5m",
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace=NAMESPACE
                ),
                path=f"./{_AIQUOTA_DIR}",
                prune=True,
                wait=True,
                # aiquota-api-bearer.sops.yaml (hand-written, listed below) is SOPS-encrypted.
                decryption=KustomizationSpecDecryption(
                    provider=KustomizationSpecDecryptionProvider.SOPS,
                    secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
                ),
                depends_on=[
                    KustomizationSpecDependsOn(name=dep)
                    for dep in (
                        "external-secrets-config",
                        "forgejo-images",
                        # Provides the shared namespace and the CLIProxyAPI management Secret.
                        "cli-proxy-api",
                        # Materializes the narrow mirrored copies of the API bearer for its
                        # consumers; the source Secret stays SOPS-managed here.
                        "external-secrets-operator",
                        # Creates the aiquota database the migrate init container populates.
                        "clickhouse-schema",
                    )
                ],
            ),
        ),
    )
    _write_yaml(
        out_dir / "kustomization.yaml",
        # aiquota-api-bearer.sops.yaml, config.toml and schema.sql stay hand-written; the
        # generator entries render the latter two into the ConfigMaps the Deployment mounts.
        # See cluster/docs/cdk8s.md.
        kustomize_kustomization(
            resources=[f"{name}.k8s.yaml", f"{aiquota_constructs.BEARER_SECRET_NAME}.sops.yaml"],
            components=["./image-pins"],
            config_map_generator=[aiquota_constructs.CONFIG_CONFIG_MAP, aiquota_constructs.SCHEMA_CONFIG_MAP],
        ),
    )


def _agentplane_health_checks(chart: Chart, namespace: str) -> list[KustomizationSpecHealthChecks]:
    checks = health_checks(chart, _HEALTH_CHECK_KINDS)
    # trust-manager names a Bundle's target ConfigMap after the Bundle.
    return [
        *checks,
        *(
            KustomizationSpecHealthChecks(api_version="v1", kind="ConfigMap", name=check.name, namespace=namespace)
            for check in checks
            if check.kind == "Bundle"
        ),
    ]


def _sops_decryption(resources: Sequence[str]) -> KustomizationSpecDecryption | None:
    """The `decryption` block a directory listing a hand-written `.sops.yaml` needs; without it
    Flux applies the ENC[...] ciphertext literally (cluster/docs/cdk8s.md)."""
    if not any(resource.endswith(".sops.yaml") for resource in resources):
        return None
    return KustomizationSpecDecryption(
        provider=KustomizationSpecDecryptionProvider.SOPS,
        secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
    )


def _generate_haku_console(root: Path) -> None:
    """Each of the console's Kustomization directories: its chart, the Flux Kustomization
    (health checks from the chart's own objects), and the root Kustomization listing the
    generated file beside the hand-written siblings."""
    for directory in haku_charts.DIRECTORIES:
        out_dir = root / directory.path
        out_dir.mkdir(parents=True, exist_ok=True)
        app = App(outdir=str(out_dir))
        chart = haku_charts.chart(app, directory)
        app.synth()
        _write_yaml(
            out_dir / "flux-kustomization.yaml",
            flux_kustomization(
                directory.name,
                spec=KustomizationSpec(
                    interval="10m",
                    retry_interval="1m",
                    timeout=directory.timeout,
                    path=f"./{directory.path}",
                    prune=True,
                    wait=True,
                    source_ref=KustomizationSpecSourceRef(
                        kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=directory.name, namespace=NAMESPACE
                    ),
                    decryption=_sops_decryption(directory.extra_resources),
                    health_checks=health_checks(chart, directory.health_check_kinds) or None,
                    depends_on=[KustomizationSpecDependsOn(name=dep) for dep in directory.depends_on],
                ),
            ),
        )
        _write_yaml(
            out_dir / "kustomization.yaml",
            kustomize_kustomization(
                namespace=directory.namespace,
                resources=[f"{directory.name}.k8s.yaml", *directory.extra_resources],
                components=["./image-pins"] if directory.image_pins else (),
                config_map_generator=directory.config_map_generator,
            ),
        )


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
                decryption=_sops_decryption(env.extra_resources),
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
    `_write_charts` (writes it to disk) and tests (in-memory synth)."""
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=metadata(configmap_name, namespace), data=data)
    return chart


def _write_charts(root: Path, app_dir: str, *chart_builders: Callable[[App], Chart]) -> None:
    """Synthesize each builder's chart into `app_dir` as `<chart id>.k8s.yaml`; the directory's
    `flux-kustomization.yaml` and `kustomization.yaml` stay hand-written (cluster/docs/cdk8s.md
    § Three shapes of a directory).
    """
    out_dir = root / app_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    for build in chart_builders:
        build(app)
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


def _public_coder_agent_config_chart(app: App) -> Chart:
    return _build_config_map_chart(
        app,
        chart_name="public-coder-agent-config",
        configmap_name="public-coder-agent-config",
        namespace="public-coder-agent",
        data={"openclaw.json5": json5_config(public_coder_agent_config.config())},
    )


def _descheduler_chart(app: App) -> Chart:
    chart = Chart(app, "helmrelease", disable_resource_name_hashes=True)
    descheduler_constructs.Descheduler(chart, "descheduler")
    return chart


def _stateful_infra_priority_class_chart(app: App) -> Chart:
    chart = Chart(app, "priorityclass", disable_resource_name_hashes=True)
    stateful_infra.priority_class(chart)
    return chart


def _dns_records_chart(app: App) -> Chart:
    """Route 53 records for allegedly.works (tf/gitops/dns-records)."""
    chart = Chart(app, "dns-records", disable_resource_name_hashes=True)
    terraform_constructs.gitops_terraform(
        chart,
        "terraform",
        name="dns-records",
        variables={"route53_zone_id": "Z02901943N8ZFQFOD9P5I"},
        env_from=[terraform_constructs.secret_env_from("aws-route53-credentials")],
    )
    return chart


def _generate_etcd_monitoring(root: Path, mesh: nebula_mesh.Mesh) -> None:
    name = "etcd-monitoring"
    out_dir = root / _ETCD_MONITORING_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = Chart(app, name, disable_resource_name_hashes=True)
    TalosEtcdMetrics(chart, "etcd", mesh)
    app.synth()

    _write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            name,
            spec=KustomizationSpec(
                interval="10m",
                retry_interval="1m",
                timeout="2m",
                path=f"./{_ETCD_MONITORING_DIR}",
                prune=True,
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="monitoring-etcd", namespace=NAMESPACE
                ),
                depends_on=[KustomizationSpecDependsOn(name="monitoring-crds")],  # the ServiceMonitor CRD
                wait=True,
                health_checks=health_checks(chart, ("ServiceMonitor",)),
            ),
        ),
    )
    _write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(namespace=etcd_constructs.NAMESPACE, resources=[f"{name}.k8s.yaml"]),
    )


def _generate_dns_records_nodes(root: Path, mesh: nebula_mesh.Mesh) -> None:
    """The roster's public Kubernetes nodes, for tf/gitops/dns-records' record sets.

    Terraform there cannot `file()` the roster itself: the tofu-controller runs from the
    `ducktape` GitRepository, a sparse checkout of deployment directories that cannot
    carry a repo-root file.
    """
    out_dir = root / _DNS_RECORDS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes = {
        name: {"public_ip": host.public_ip, "role": host.role}
        for name, host in sorted(mesh.public_kubernetes_nodes().items())
    }
    (out_dir / "public-nodes.json").write_text(json.dumps(nodes, indent=2) + "\n")


def generate_manifests(root: Path) -> None:
    """Write every converted directory's generated manifests under `root`."""
    mesh = nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))
    _generate_litellm_app(root)
    _generate_ha_mcp(root)
    _generate_clickhouse_schema(root)
    _generate_aiquota(root)
    for env in (staging.ENV, testing.ENV):
        _generate_agentplane(root, env)
    _generate_haku_console(root)
    _write_charts(root, _HAKU_OPENCLAW_SPIKE_APP_DIR, _haku_openclaw_spike_config_chart)
    _write_charts(root, _PUBLIC_CODER_AGENT_APP_DIR, _public_coder_agent_config_chart)
    _write_charts(root, _DESCHEDULER_DIR, _descheduler_chart)
    _write_charts(root, _SEAWEEDFS_CLUSTER_DIR, _stateful_infra_priority_class_chart)
    _write_charts(
        root,
        _HAKU_EGRESS_PROXY_DIR,
        egress_fences.haku_cloud_api,
        egress_fences.haku_claude,
        egress_fences.haku_openclaw_spike,
    )
    _write_charts(root, _MITMPROXY_DIR, egress_fences.mitmproxy_cloud_api)
    _write_charts(root, _DNS_AUTOMATION_DIR, _dns_records_chart)
    _write_charts(root, _LITELLM_KEYS_TF_DIR, _litellm_keys_chart)
    _generate_etcd_monitoring(root, mesh)
    _generate_dns_records_nodes(root, mesh)


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
