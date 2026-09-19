"""The console's three Flux Kustomizations -- database, migration gate, and the console
itself with its Kubernetes API proxy -- each as one chart, shared by generate_manifests
(writes them to disk) and the tests (synthesize them in memory via `cdk8s.Testing`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from cdk8s import App, Chart

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux_constructs import ConfigMapArgs
from cluster.cdk8s.haku import console_constructs, db_constructs
from cluster.cdk8s.haku.console_constructs import Console
from cluster.cdk8s.haku.db_constructs import Db
from cluster.cdk8s.haku.kube_api_proxy_constructs import KubeApiProxy
from cluster.cdk8s.haku.migration_constructs import Migration

_NAMESPACE_KUSTOMIZATION = "haku-console-namespace"


@dataclass(frozen=True)
class Directory:
    """One Flux Kustomization directory: what it renders and what it waits on."""

    # The Flux Kustomization, chart and `<name>.k8s.yaml`.
    name: str
    path: str
    depends_on: tuple[str, ...]
    # Secrets and ConfigMaps Pods read that the chart does not create, each with the
    # dependency or sibling file that does; fleet_rules checks both ends.
    provided_secrets: Mapping[str, str]
    timeout: str
    provided_config_maps: Mapping[str, str] = field(default_factory=dict)
    # Hand-written files listed beside the generated one.
    extra_resources: tuple[str, ...] = ()
    config_map_generator: tuple[ConfigMapArgs, ...] = ()
    # Whether a hand-written image-pins/ Component overrides the placeholder image tags.
    image_pins: bool = False
    # The kustomization.yaml `namespace`, which the configMapGenerator output needs.
    namespace: str | None = None
    # Kinds whose readiness the Flux Kustomization lists explicitly on top of `wait: true`.
    health_check_kinds: tuple[str, ...] = ()


def _chart(app: App, directory: Directory) -> Chart:
    """The directory's empty chart with its fleet rules attached. The rules are a
    synth-time validation, so the caller fills the chart afterwards."""
    chart = Chart(app, directory.name, disable_resource_name_hashes=True)
    add_fleet_rules(
        chart,
        provided_secrets=directory.provided_secrets,
        provided_config_maps=directory.provided_config_maps,
        providers=frozenset(
            {
                *directory.depends_on,
                *directory.extra_resources,
                *(file for generator in directory.config_map_generator for file in generator.files),
            }
        ),
    )
    return chart


DB = Directory(
    name="haku-console-db",
    path="cluster/k8s/haku/console/db",
    depends_on=(
        _NAMESPACE_KUSTOMIZATION,
        "cnpg",
        "local-path-provisioner",
        # The haku_indexer managed-role password is an ESO-generated Secret.
        "external-secrets-config",
    ),
    provided_secrets={},
    timeout="5m",
)

MIGRATION = Directory(
    name="haku-console-migration",
    path="cluster/k8s/haku/console/migration",
    depends_on=(
        # The namespace layer ships the forgejo-images-creds ExternalSecret the Job pulls its
        # private image with, from the ClusterSecretStore forgejo-images provides.
        _NAMESPACE_KUSTOMIZATION,
        "forgejo-images",
        DB.name,
    ),
    provided_secrets={"forgejo-images-creds": _NAMESPACE_KUSTOMIZATION, db_constructs.APP_SECRET: DB.name},
    timeout="10m",
    image_pins=True,
    namespace=console_constructs.NAMESPACE,
    health_check_kinds=("Job",),
)

CONSOLE = Directory(
    name="haku-console",
    path="cluster/k8s/haku/console",
    depends_on=(
        # Runtime namespace/template changes must become Ready before the console starts
        # creating claims against their new namespace: a namespace migration fails closed
        # instead of opening a window where the API points at a pool Flux has not reconciled.
        "haku-workspaces",
        # The fixed-name Job runs Alembic from the exact API image and reports completion
        # before the API or static workloads roll.
        MIGRATION.name,
        # Already gating the migration; listed so the Secrets the Pods read name their
        # provider: the registry credential and the database's app credential.
        _NAMESPACE_KUSTOMIZATION,
        DB.name,
        # Provisions the shared haku-ui/backend -> haku-console MCP static-Agent token.
        "haku-state",
        "gateway",
        # TF mints the console's Authentik OAuth2 providers and the haku-console-oidc Secret
        # (operator_oidc + mcp_oauth client credentials + session secret); the pod's
        # build_authentik_auth does a synchronous OIDC discovery fetch at startup, so the
        # provider must exist first. That Kustomization itself depends on Authentik.
        "agent-machine-access-tf",
        # Copies the MCP backends' bearers and aiquota's into this namespace.
        "reflector",
        "external-creds",
        "external-secrets-config",
        # The SSH MCP backend the console fronts.
        "ssh-mcp",
        # The ServiceMonitor CRD.
        "monitoring-crds",
    ),
    provided_secrets={
        "forgejo-images-creds": _NAMESPACE_KUSTOMIZATION,
        db_constructs.APP_SECRET: DB.name,
        "haku-console-oidc": "agent-machine-access-tf",
        "haku-console-public-coder-agent": "agent-machine-access-tf",
        "haku-console-agent-api": "haku-state",
        # Reflected from the namespaces of tana-mcp, ha-mcp, ssh-mcp and aiquota.
        "tana-agentydragon-gmail-com-account-pat": "reflector",
        "ha-mcp-bearer": "reflector",
        "ssh-mcp-bearer": "reflector",
        "aiquota-api-bearer-haku-console": "reflector",
        "haku-routine-launch-token": "routine-launch-token.sops.yaml",
        "haku-console-web-push-vapid": "web-push-vapid.sops.yaml",
        "haku-console-google-client-credentials": "haku-console-google-client-credentials.sops.yaml",
        "haku-console-google-calendar-client-credentials": "haku-console-google-calendar-client-credentials.sops.yaml",
        "haku-console-github-mcp-client-credentials": "haku-console-github-mcp-client-credentials.sops.yaml",
    },
    timeout="5m",
    provided_config_maps={
        console_constructs.STATIC_METADATA_CONFIG_MAP: "static-metadata.yaml",
        console_constructs.IMAGE_METADATA_CONFIG_MAP: "image-metadata.yaml",
        console_constructs.INDEXER_SQL_CONFIG_MAP: "indexer-role.sql",
    },
    extra_resources=(
        "haku-console-google-calendar-client-credentials.sops.yaml",
        "haku-console-google-client-credentials.sops.yaml",
        "haku-console-github-mcp-client-credentials.sops.yaml",
        "routine-launch-token.sops.yaml",
        "web-push-vapid.sops.yaml",
        "static-metadata.yaml",
        "image-metadata.yaml",
    ),
    config_map_generator=(
        ConfigMapArgs(
            name=console_constructs.INDEXER_SQL_CONFIG_MAP,
            namespace=console_constructs.NAMESPACE,
            files=["indexer-role.sql"],
        ),
    ),
    image_pins=True,
    namespace=console_constructs.NAMESPACE,
)


def db_chart(app: App) -> Chart:
    chart = _chart(app, DB)
    Db(chart, "db")
    return chart


def migration_chart(app: App) -> Chart:
    chart = _chart(app, MIGRATION)
    Migration(chart, "migration")
    return chart


def console_chart(app: App) -> Chart:
    chart = _chart(app, CONSOLE)
    Console(chart, "console")
    KubeApiProxy(chart, "kube-api-proxy")
    return chart
