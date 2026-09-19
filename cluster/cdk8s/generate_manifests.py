"""Synthesize each converted directory's manifests with Python cdk8s, writing
them directly into their `cluster/k8s` directory.

Every generated Deployment/Job/CronJob carries a placeholder image tag -- each
environment's own hand-written `image-pins/kustomization.yaml` Kustomize
Component (never written by this generator) carries Flux's `$imagepolicy`
marker and overrides the real tag at `kustomize build` time. See
cluster/docs/cdk8s.md.
"""

from collections.abc import Sequence
from itertools import groupby
from pathlib import Path

from cdk8s import App, Chart, Yaml
from cdk8s_plus_34 import ConfigMap
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s import (
    aiquota_constructs,
    clickhouse_schema_constructs,
    descheduler_constructs,
    egress_fences,
    etcd_constructs,
    ha_mcp_constructs,
    haku_openclaw_spike_config,
    litellm_constructs,
    public_coder_agent_config,
    stateful_infra,
    terraform_constructs,
)
from cluster.cdk8s.agentplane import staging, testing
from cluster.cdk8s.agentplane.chart import environment_directory
from cluster.cdk8s.config_format import json5_config
from cluster.cdk8s.directory import Directory, chart
from cluster.cdk8s.flux_constructs import NAMESPACE, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.haku import charts as haku_charts
from cluster.cdk8s.litellm_keys import model_allowlists
from cluster.cdk8s.metadata import metadata
from cluster.scripts import nebula_mesh
from cluster.scripts.nebula_mesh import Mesh
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory

_LITELLM_APP_DIR = "cluster/k8s/litellm/app"
_LITELLM_KEYS_TF_DIR = "cluster/k8s/litellm/keys-tf"
_HA_MCP_DIR = "cluster/k8s/agents/ha-mcp/app"
_CLICKHOUSE_SCHEMA_DIR = "cluster/k8s/clickhouse/schema"
_AIQUOTA_DIR = "cluster/k8s/aiquota"
_DNS_AUTOMATION_DIR = "cluster/k8s/dns-automation"
_ETCD_MONITORING_DIR = "cluster/k8s/monitoring/etcd"
_HAKU_OPENCLAW_SPIKE_APP_DIR = "cluster/k8s/agents/haku-openclaw-spike/app"
_PUBLIC_CODER_AGENT_APP_DIR = "cluster/k8s/agents/public-coder-agent/app"
_DESCHEDULER_DIR = "cluster/k8s/descheduler"
_SEAWEEDFS_CLUSTER_DIR = "cluster/k8s/seaweedfs/cluster"
_HAKU_EGRESS_PROXY_DIR = "cluster/k8s/agents/haku-egress-proxy"
_MITMPROXY_DIR = "cluster/k8s/agents/mitmproxy"


def _write_yaml(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(Yaml.format_objects([manifest]))


def _sops_decryption(resources: Sequence[str]) -> KustomizationSpecDecryption | None:
    """The `decryption` block a directory listing a hand-written `.sops.yaml` needs; without it
    Flux applies the ENC[...] ciphertext literally (cluster/docs/cdk8s.md)."""
    if not any(resource.endswith(".sops.yaml") for resource in resources):
        return None
    return KustomizationSpecDecryption(
        provider=KustomizationSpecDecryptionProvider.SOPS,
        secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
    )


def _write_directories(root: Path, directories: Sequence[Directory]) -> None:
    """Synthesize every directory sharing one `path` into a single `App`/`app.synth()`
    call -- cdk8s writes one file per chart, so a hand-written directory whose several
    generated charts must land together (`_HAKU_EGRESS_PROXY_DIR`'s fences) is one group
    of `Directory` entries, not several independent synth passes. Each `generate_flux`
    directory then gets its own `flux-kustomization.yaml`/`kustomization.yaml`.
    """
    out_dir = root / directories[0].path
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    built = [(directory, chart(app, directory)) for directory in directories]
    app.synth()
    for directory, built_chart in built:
        if directory.generate_flux:
            _write_flux_wiring(out_dir, built_chart, directory)


def _write_flux_wiring(out_dir: Path, built_chart: Chart, directory: Directory) -> None:
    chart_id = built_chart.node.id
    _write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            directory.name,
            description=directory.description,
            spec=KustomizationSpec(
                interval=directory.interval,
                retry_interval=directory.retry_interval,
                timeout=directory.timeout,
                path=f"./{directory.path}",
                prune=True,
                wait=directory.wait,
                deletion_policy=directory.deletion_policy,
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                    name=directory.source_ref_name or directory.name,
                    namespace=NAMESPACE,
                ),
                decryption=_sops_decryption(directory.extra_resources),
                health_checks=directory.health_checks(built_chart) or None,
                health_check_exprs=list(directory.health_check_exprs) or None,
                depends_on=[KustomizationSpecDependsOn(name=dep) for dep in directory.depends_on] or None,
            ),
        ),
    )
    _write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(
            namespace=directory.namespace,
            resources=[f"{chart_id}.k8s.yaml", *directory.extra_resources],
            components=["./image-pins"] if directory.image_pins else (),
            config_map_generator=directory.config_map_generator,
        ),
    )


def _litellm_app_directory() -> Directory:
    (spec,) = litellm_constructs.proxy_specs()  # one proxy today; extend proxy_specs() when a second lands
    return Directory(
        name=spec.name,
        path=_LITELLM_APP_DIR,
        build=lambda app: litellm_constructs.app_chart(app, spec),
        depends_on=(
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
        ),
        provided_secrets={
            "litellm-master-key": "litellm-secrets",
            "litellm-salt-key": "litellm-secrets",
            "litellm-anthropic-key": "litellm-secrets",
            "litellm-groq-key": "litellm-secrets",
            "litellm-gemini-key": "litellm-secrets",
            "litellm-mistral-key": "litellm-secrets",
            "litellm-cliproxy-key": "litellm-secrets",
            "litellm-db-app": "litellm-db",
            "langfuse-secrets": "langfuse-secrets",
            "tana-firebase-refresh-token": "tana-mcp",
        },
        timeout="10m",
        retry_interval=None,
        wait=None,
        namespace="litellm",
        image_pins=True,
    )


def _ha_mcp_directory() -> Directory:
    return Directory(
        name=ha_mcp_constructs.NAME,
        path=_HA_MCP_DIR,
        build=ha_mcp_constructs.chart,
        depends_on=(
            "external-secrets-config",
            "forgejo-images",
            "home-assistant",
            "monitoring-crds",  # the ServiceMonitor CRD
        ),
        provided_secrets={
            "home-assistant-break-glass": "home-assistant",
            # bearer.sops.yaml (hand-written, listed below) is SOPS-encrypted; without a
            # decryption block Flux applies the ENC[...] ciphertext literally.
            "ha-mcp-bearer": "bearer.sops.yaml",
            # Created imperatively by this directory's own token-provisioner Job, not by any
            # static manifest -- `directory.chart()` always accepts a directory as its own
            # provider.
            "ha-mcp-home-assistant-token": ha_mcp_constructs.NAME,
        },
        extra_resources=("bearer.sops.yaml",),
        timeout="5m",
        health_check_kinds=("Job", "Deployment"),
        image_pins=True,
    )


def _clickhouse_schema_directory() -> Directory:
    name = clickhouse_schema_constructs.NAME
    return Directory(
        name=name,
        path=_CLICKHOUSE_SCHEMA_DIR,
        build=clickhouse_schema_constructs.chart,
        depends_on=("clickhouse",),
        provided_secrets={"clickhouse-admin-credentials": "clickhouse"},
        timeout="20m",
        health_check_kinds=("Job",),
        # schema.sql stays hand-written; this generator entry renders it into the ConfigMap
        # the Job mounts. See cluster/docs/cdk8s.md.
        config_map_generator=(clickhouse_schema_constructs.SCHEMA_CONFIG_MAP,),
    )


def _aiquota_directory() -> Directory:
    name = aiquota_constructs.NAME
    return Directory(
        name=name,
        path=_AIQUOTA_DIR,
        build=aiquota_constructs.chart,
        description="aiquota API with Claude and Codex quota through the CLIProxyAPI integration.",
        depends_on=(
            "external-secrets-config",
            "forgejo-images",
            # Provides the shared namespace and the CLIProxyAPI management Secret.
            "cli-proxy-api",
            # Materializes the narrow mirrored copies of the API bearer for its consumers;
            # the source Secret stays SOPS-managed here.
            "external-secrets-operator",
            # Creates the aiquota database the migrate init container populates.
            "clickhouse-schema",
            # Mints the aiquota-oidc Authentik OAuth2 client credentials Secret.
            "agent-machine-access-tf",
            # Reflects clickhouse-aiquota-credentials from the clickhouse namespace.
            "reflector",
        ),
        provided_secrets={
            aiquota_constructs.BEARER_SECRET_NAME: f"{aiquota_constructs.BEARER_SECRET_NAME}.sops.yaml",
            "cli-proxy-api-management": "cli-proxy-api",
            "aiquota-oidc": "agent-machine-access-tf",
            "clickhouse-aiquota-credentials": "reflector",
        },
        # aiquota-api-bearer.sops.yaml (hand-written, listed below) is SOPS-encrypted.
        extra_resources=(f"{aiquota_constructs.BEARER_SECRET_NAME}.sops.yaml",),
        timeout="5m",
        # aiquota-api-bearer.sops.yaml, config.toml and schema.sql stay hand-written; the
        # generator entries render the latter two into the ConfigMaps the Deployment mounts.
        # See cluster/docs/cdk8s.md.
        config_map_generator=(aiquota_constructs.CONFIG_CONFIG_MAP, aiquota_constructs.SCHEMA_CONFIG_MAP),
        image_pins=True,
    )


def _etcd_monitoring_directory(mesh: Mesh) -> Directory:
    return Directory(
        name=etcd_constructs.NAME,
        path=_ETCD_MONITORING_DIR,
        build=lambda app: etcd_constructs.chart(app, mesh),
        depends_on=("monitoring-crds",),  # the ServiceMonitor CRD
        timeout="2m",
        health_check_kinds=("ServiceMonitor",),
        namespace=etcd_constructs.NAMESPACE,
        source_ref_name="monitoring-etcd",
    )


def _build_config_map_chart(
    app: App, *, chart_name: str, configmap_name: str, namespace: str, data: dict[str, str]
) -> Chart:
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=metadata(configmap_name, namespace), data=data)
    return chart


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


def _dns_records_chart(app: App, mesh: Mesh) -> Chart:
    """Route 53 records for allegedly.works (tf/gitops/dns-records)."""
    chart = Chart(app, "dns-records", disable_resource_name_hashes=True)
    terraform_constructs.gitops_terraform(
        chart,
        "terraform",
        name="dns-records",
        variables={
            "route53_zone_id": "Z02901943N8ZFQFOD9P5I",
            # Inline rather than a ConfigMap read through varsFrom: tofu-controller writes
            # spec.vars structurally into the runner's tfvars (a varsFrom value arrives as one
            # string) and reconciles a spec change at once, while a referenced ConfigMap is
            # never watched and waits for the interval.
            "public_nodes": {
                name: {"public_ip": host.public_ip, "role": host.role}
                for name, host in sorted(mesh.public_kubernetes_nodes().items())
            },
        },
        env_from=[terraform_constructs.secret_env_from("aws-route53-credentials")],
    )
    return chart


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


def _shape_two_directories(mesh: Mesh) -> tuple[Directory, ...]:
    """Directories that generate only their `.k8s.yaml` file(s) beside a hand-written
    `flux-kustomization.yaml`/`kustomization.yaml` (cluster/docs/cdk8s.md § Three shapes
    of a directory, shape 2)."""
    return (
        Directory(
            name="haku-openclaw-spike-config",
            path=_HAKU_OPENCLAW_SPIKE_APP_DIR,
            build=_haku_openclaw_spike_config_chart,
            generate_flux=False,
        ),
        Directory(
            name="public-coder-agent-config",
            path=_PUBLIC_CODER_AGENT_APP_DIR,
            build=_public_coder_agent_config_chart,
            generate_flux=False,
        ),
        Directory(name="descheduler", path=_DESCHEDULER_DIR, build=_descheduler_chart, generate_flux=False),
        Directory(
            name="seaweedfs-cluster-priorityclass",
            path=_SEAWEEDFS_CLUSTER_DIR,
            build=_stateful_infra_priority_class_chart,
            generate_flux=False,
        ),
        # The three fences below all live in _HAKU_EGRESS_PROXY_DIR: kept adjacent so
        # `_write_directories` synthesizes them together into that one directory.
        #
        # None of the four fences here pin HTTPS SNI: `cilium.fqdn_fence` deliberately
        # never sets `serverNames` (a group may hold wildcard patterns SNI can't carry --
        # its own docstring), and haku_claude/haku_openclaw_spike additionally reach
        # remote-node/host/world on 443 for destinations toFQDNs cannot select by node
        # identity (their own docstrings). Each is still bounded, by the DNS-layer fence
        # and, for haku_openclaw_spike, the iron proxy's own L7 allowlist.
        Directory(
            name="cnp-haku-cloud-api-egress",
            path=_HAKU_EGRESS_PROXY_DIR,
            build=egress_fences.haku_cloud_api,
            unpinned_https_egress=("allow-haku-cloud-api-egress",),
            generate_flux=False,
        ),
        Directory(
            name="cnp-haku-claude-egress",
            path=_HAKU_EGRESS_PROXY_DIR,
            build=egress_fences.haku_claude,
            unpinned_https_egress=("allow-haku-claude-oauth-proxy-egress",),
            generate_flux=False,
        ),
        Directory(
            name="openclaw-spike-cnp-egress",
            path=_HAKU_EGRESS_PROXY_DIR,
            build=egress_fences.haku_openclaw_spike,
            unpinned_https_egress=("allow-haku-openclaw-spike-proxy-egress",),
            generate_flux=False,
        ),
        Directory(
            name="cnp-cloud-api-egress",
            path=_MITMPROXY_DIR,
            build=egress_fences.mitmproxy_cloud_api,
            unpinned_https_egress=("allow-cloud-api-egress",),
            generate_flux=False,
        ),
        Directory(
            name="dns-records",
            path=_DNS_AUTOMATION_DIR,
            build=lambda app: _dns_records_chart(app, mesh),
            generate_flux=False,
        ),
        Directory(name="litellm-keys", path=_LITELLM_KEYS_TF_DIR, build=_litellm_keys_chart, generate_flux=False),
    )


def generate_manifests(root: Path) -> None:
    """Write every converted directory's generated manifests under `root`."""
    mesh = nebula_mesh.load(get_required_path("_main/nebula-mesh.json"))
    directories = (
        _litellm_app_directory(),
        _ha_mcp_directory(),
        _clickhouse_schema_directory(),
        _aiquota_directory(),
        *(environment_directory(env) for env in (staging.ENV, testing.ENV)),
        *haku_charts.DIRECTORIES,
        _etcd_monitoring_directory(mesh),
        *_shape_two_directories(mesh),
    )
    for _, group in groupby(directories, key=lambda directory: directory.path):
        _write_directories(root, list(group))


def main() -> None:
    generate_manifests(get_build_workspace_directory())


if __name__ == "__main__":
    main()
