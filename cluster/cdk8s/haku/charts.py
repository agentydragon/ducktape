"""The console's three Flux Kustomizations -- database, migration gate, and the console
itself with its Kubernetes API proxy -- each as one `directory.Directory`, shared by
generate_manifests (writes them to disk) and the tests (synthesize them in memory via
`cdk8s.Testing`).
"""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.directory import Directory
from cluster.cdk8s.flux_constructs import ConfigMapArgs
from cluster.cdk8s.haku import console_constructs, db_constructs
from cluster.cdk8s.haku.console_constructs import Console
from cluster.cdk8s.haku.db_constructs import Db
from cluster.cdk8s.haku.kube_api_proxy_constructs import KubeApiProxy
from cluster.cdk8s.haku.migration_constructs import Migration

_NAMESPACE_KUSTOMIZATION = "haku-console-namespace"

_DB_NAME = "haku-console-db"
_MIGRATION_NAME = "haku-console-migration"
_CONSOLE_NAME = "haku-console"


def _db_chart(app: App) -> Chart:
    chart = Chart(app, _DB_NAME, disable_resource_name_hashes=True)
    Db(chart, "db")
    return chart


def _migration_chart(app: App) -> Chart:
    chart = Chart(app, _MIGRATION_NAME, disable_resource_name_hashes=True)
    Migration(chart, "migration")
    return chart


def _console_chart(app: App) -> Chart:
    chart = Chart(app, _CONSOLE_NAME, disable_resource_name_hashes=True)
    Console(chart, "console")
    KubeApiProxy(chart, "kube-api-proxy")
    return chart


DB = Directory(
    name=_DB_NAME,
    path="cluster/k8s/haku/console/db",
    build=_db_chart,
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
    name=_MIGRATION_NAME,
    path="cluster/k8s/haku/console/migration",
    build=_migration_chart,
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
    name=_CONSOLE_NAME,
    path="cluster/k8s/haku/console",
    build=_console_chart,
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
        # INDEXER_SQL_CONFIG_MAP isn't listed: `directory.chart()` derives it automatically
        # from `config_map_generator` below -- a `config_map_generator` entry always
        # provides its own ConfigMap.
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

DIRECTORIES = (DB, MIGRATION, CONSOLE)
