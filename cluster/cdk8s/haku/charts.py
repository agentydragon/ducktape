"""The console's whole Flux Kustomization as one chart -- database, schema migration, the
console itself and its Kubernetes API proxy -- shared by generate_manifests (writes it to
disk) and the tests (synthesize it in memory via `cdk8s.Testing`).

The database and migration used to be Kustomizations of their own, ordered ahead of the
console by `dependsOn`. One Kustomization has no such ordering, so the two Jobs in here
retry until their preconditions hold (migration.py, `_add_indexer_provisioner`)
rather than relying on the layer beneath them already being Ready.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    NAMESPACE as FLUX_NAMESPACE,
    ConfigMapArgs,
    flux_kustomization,
    health_checks,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption, write_yaml
from cluster.cdk8s.haku import console
from cluster.cdk8s.haku.console import Console
from cluster.cdk8s.haku.database import Db
from cluster.cdk8s.haku.kube_api_proxy import KubeApiProxy
from cluster.cdk8s.haku.migration import Migration
from cluster.cdk8s.ssh_mcp.config import BEARER_SECRET_NAME

_NAMESPACE_KUSTOMIZATION = "haku-console-namespace"

NAME = console.NAME
NAMESPACE = console.NAMESPACE
PATH = "cluster/k8s/haku/console"
# Long enough for the slowest cold path -- CNPG bootstrapping a fresh two-instance Cluster,
# then the migration and the GRANTs converging on their retries behind it.
TIMEOUT = "20m"

DEPENDS_ON = (
    # Runtime namespace/template changes must become Ready before the console starts
    # creating claims against their new namespace: a namespace migration fails closed
    # instead of opening a window where the API points at a pool Flux has not reconciled.
    "haku-workspaces",
    # Ships the namespace itself and the forgejo-images-creds ExternalSecret the private
    # images are pulled with.
    _NAMESPACE_KUSTOMIZATION,
    # The operator behind the Cluster, and the storage class its PVCs bind.
    "cnpg",
    "local-path-provisioner",
    "forgejo-images",
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
)

# Hand-written files the root Kustomization lists beside the generated one.
EXTRA_RESOURCES = (
    "haku-console-google-calendar-client-credentials.sops.yaml",
    "haku-console-google-client-credentials.sops.yaml",
    "haku-console-github-mcp-client-credentials.sops.yaml",
    "routine-launch-token.sops.yaml",
    "web-push-vapid.sops.yaml",
    "static-metadata.yaml",
    "image-metadata.yaml",
)

CONFIG_MAP_GENERATOR = (
    ConfigMapArgs(name=console.INDEXER_SQL_CONFIG_MAP, namespace=console.NAMESPACE, files=["indexer-role.sql"]),
)

# Secrets and ConfigMaps Pods read that the chart does not create, each with the dependency
# or sibling file that does; fleet_rules checks both ends. The CNPG app credential is absent
# because the Cluster minting it now lives in this same chart.
_PROVIDED_SECRETS = {
    "forgejo-images-creds": _NAMESPACE_KUSTOMIZATION,
    "haku-console-oidc": "agent-machine-access-tf",
    "haku-console-public-coder-agent": "agent-machine-access-tf",
    "haku-console-agent-api": "haku-state",
    # Reflected from the namespaces of tana-mcp, ha-mcp, ssh-mcp and aiquota.
    "tana-agentydragon-gmail-com-account-pat": "reflector",
    "ha-mcp-bearer": "reflector",
    BEARER_SECRET_NAME: "reflector",
    "aiquota-api-bearer-haku-console": "reflector",
    "haku-routine-launch-token": "routine-launch-token.sops.yaml",
    "haku-console-web-push-vapid": "web-push-vapid.sops.yaml",
    "haku-console-google-client-credentials": "haku-console-google-client-credentials.sops.yaml",
    "haku-console-google-calendar-client-credentials": "haku-console-google-calendar-client-credentials.sops.yaml",
    "haku-console-github-mcp-client-credentials": "haku-console-github-mcp-client-credentials.sops.yaml",
}

_PROVIDED_CONFIG_MAPS = {
    console.STATIC_METADATA_CONFIG_MAP: "static-metadata.yaml",
    console.IMAGE_METADATA_CONFIG_MAP: "image-metadata.yaml",
    console.INDEXER_SQL_CONFIG_MAP: "indexer-role.sql",
}


def console_chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Db(chart, "db")
    Migration(chart, "migration")
    Console(chart, "console")
    KubeApiProxy(chart, "kube-api-proxy")
    add_fleet_rules(
        chart,
        provided_secrets=_PROVIDED_SECRETS,
        provided_config_maps=_PROVIDED_CONFIG_MAPS,
        providers=frozenset(
            {*DEPENDS_ON, *EXTRA_RESOURCES, *(file for generator in CONFIG_MAP_GENERATOR for file in generator.files)}
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    """The console's one Kustomization directory: database, migration, console and API
    proxy in a single chart, its Flux Kustomization (health checks from the chart's own
    objects), and the root Kustomization listing the generated file beside the
    hand-written siblings."""
    out_dir = root / PATH
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = console_chart(app)
    app.synth()
    write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            NAME,
            spec=KustomizationSpec(
                interval="10m",
                retry_interval="1m",
                timeout=TIMEOUT,
                path=f"./{PATH}",
                prune=True,
                # This one Kustomization owns the CNPG Cluster's PVCs; pruning on deletion
                # would take the console's approval ledger with them.
                deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
                wait=True,
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=NAME, namespace=FLUX_NAMESPACE
                ),
                decryption=sops_decryption(EXTRA_RESOURCES),
                # The two Jobs gate every dependent Kustomization: nothing downstream
                # reconciles until the schema is migrated and the indexer GRANTs applied.
                health_checks=health_checks(chart, ("Cluster", "Job")),
                health_check_exprs=[
                    KustomizationSpecHealthCheckExprs(
                        api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
                    )
                ],
                depends_on=[KustomizationSpecDependsOn(name=dep) for dep in DEPENDS_ON],
            ),
        ),
    )
    write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(
            namespace=NAMESPACE,
            resources=[f"{NAME}.k8s.yaml", *EXTRA_RESOURCES],
            components=["./image-pins"],
            config_map_generator=CONFIG_MAP_GENERATOR,
        ),
    )
