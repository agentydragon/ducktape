"""The console resource chart -- database, schema migration, the console itself and its
Kubernetes API proxy -- shared by manifest generation and tests (synthesized in memory
via `cdk8s.Testing`).

The database and migration used to be Kustomizations of their own, ordered ahead of the
console by `dependsOn`. One Kustomization has no such ordering, so the two Jobs in here
retry until their preconditions hold (migration.py, `_add_indexer_provisioner`)
rather than relying on the layer beneath them already being Ready.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    NAMESPACE as FLUX_NAMESPACE,
    ConfigMapArgs,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption, write_yaml
from cluster.cdk8s.haku import console
from cluster.cdk8s.haku.console import Console
from cluster.cdk8s.haku.database import Db
from cluster.cdk8s.haku.kube_api_proxy import KubeApiProxy
from cluster.cdk8s.haku.migration import Migration

NAME = console.NAME
NAMESPACE = console.NAMESPACE
PATH = "cluster/k8s/haku/console"
# Long enough for the slowest cold path -- CNPG bootstrapping a fresh two-instance Cluster,
# then the migration and the GRANTs converging on their retries behind it.
TIMEOUT = "20m"

# Hand-written files the root Kustomization lists beside the generated one.
EXTRA_RESOURCES = (
    "haku-console-google-calendar-client-credentials.sops.yaml",
    "haku-console-google-client-credentials.sops.yaml",
    "haku-console-github-mcp-client-credentials.sops.yaml",
    "routine-launch-token.sops.yaml",
    "web-push-vapid.sops.yaml",
    "../console-namespace",
    "static-metadata.yaml",
    "image-metadata.yaml",
)

CONFIG_MAP_GENERATOR = (
    ConfigMapArgs(name=console.INDEXER_SQL_CONFIG_MAP, namespace=console.NAMESPACE, files=["indexer-role.sql"]),
)


def console_chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Db(chart, "db")
    Migration(chart, "migration")
    Console(chart, "console")
    KubeApiProxy(chart, "kube-api-proxy")
    add_fleet_rules(chart)
    return chart


def write_console_manifests(root: Path) -> Chart:
    """Synthesize the console resource chart and its directory Kustomize config."""
    out_dir = root / PATH
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = console_chart(app)
    app.synth()
    write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(
            namespace=NAMESPACE,
            resources=[f"{NAME}.k8s.yaml", *EXTRA_RESOURCES],
            components=["./image-pins"],
            config_map_generator=CONFIG_MAP_GENERATOR,
        ),
    )
    return chart


def haku_console(
    flux_chart: Chart,
    health_checks: list[KustomizationSpecHealthChecks],
    cnpg: Kustomization,
    local_path_provisioner: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    agent_machine_access_tf: Kustomization,
    reflector: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    ssh_mcp: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    """Build the Flux graph node from health-check values and predecessor nodes."""
    return flux_kustomization(
        flux_chart,
        NAME,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout=TIMEOUT,
            # Keep the artifact nested: its kustomization reads console-namespace as a sibling.
            path="./cluster/k8s/haku/console",
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
            health_checks=health_checks,
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
                )
            ],
            # haku-state writes a credential into this namespace; gating on it (or
            # haku-workspaces -> haku-egress-proxy -> haku-state) blocks namespace
            # creation. Pods can wait for credentials after this layer is admitted.
            depends_on=flux_kustomization_depends_on_many(
                # The Cluster operator and the storage class its PVCs bind.
                cnpg,
                local_path_provisioner,
                forgejo_images,
                gateway,
                # TF creates the Authentik clients and haku-console-oidc Secret;
                # the console does OIDC discovery synchronously at startup.
                agent_machine_access_tf,
                # Copies the MCP backends' bearers and aiquota's into this namespace.
                reflector,
                external_creds,
                external_secrets_config,
                # The SSH MCP backend the console fronts and the ServiceMonitor CRD.
                ssh_mcp,
                monitoring_crds,
            ),
        ),
    )
