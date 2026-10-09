"""The console resource chart -- database, schema migration, the console itself and its
Kubernetes API proxy -- shared by manifest generation and tests (synthesized in memory
via `cdk8s.Testing`).

The database and migration used to be Kustomizations of their own, ordered ahead of the
console by `dependsOn`. One Kustomization has no such ordering, so the migration Job retries
until its preconditions hold rather than relying on the layer beneath it already being Ready.
"""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)

from cluster.cdk8s import namespaces
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_external_secret
from cluster.cdk8s.generation import CNPG_DATABASE_READY
from cluster.cdk8s.haku import console
from cluster.cdk8s.haku.console import Console
from cluster.cdk8s.haku.database import Db
from cluster.cdk8s.haku.kube_api_proxy import KubeApiProxy
from cluster.cdk8s.haku.migration import Migration
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = console.NAME
NAMESPACE = console.NAMESPACE
PATH = f"{HAND_WRITTEN_ROOT}/haku/console"
# Long enough for the slowest cold path -- CNPG bootstrapping a fresh two-instance Cluster,
# then the migration converging on its retries behind it.
TIMEOUT = "20m"

# Hand-written files the root Kustomization lists beside the generated one.
EXTRA_RESOURCES = (
    # Not read by the console: agentplane-staging copies it with ESO, and its Action Service
    # links GitHub with it (cluster/k8s/haku/console/README.md).
    "haku-console-github-mcp-client-credentials.sops.yaml",
    "routine-launch-token.sops.yaml",
    "web-push-vapid.sops.yaml",
    "static-metadata.yaml",
    "image-metadata.yaml",
)


def console_chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # The Haku console's own trusted namespace -- deliberately NOT haku-sandbox.
    # The console is reviewed/released ducktape code, so it sits OUTSIDE Haku's
    # RBAC (no `haku` RoleBinding here) and OUTSIDE the haku-egress-proxy egress fence
    # (the fence's CiliumClusterwideNetworkPolicy keys on the haku-sandbox
    # namespace; this one gets ordinary egress). That is the confidentiality boundary
    # letting the console hold secrets Haku may not read, e.g. the Claude Code web
    # session bearer.
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO, labels={"name": NAMESPACE})
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    # The static-Agent bearer tf/gitops/haku-state mints; Reflector mirrors it on to the
    # Agent's callers.
    secret_copy.secret_copy(
        chart,
        "haku-console-agent-api",
        reader=secret_copy.reader(chart, NAMESPACE),
        mirror_namespaces=["haku-sandbox", "haku-egress-proxy"],
    )
    Db(chart, "db")
    Migration(chart, "migration")
    Console(chart, "console")
    KubeApiProxy(chart, "kube-api-proxy")
    add_fleet_rules(chart)
    return chart


def haku_console(
    flux_chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    """Build the Flux graph node from its predecessor nodes."""
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout=TIMEOUT,
        # This one Kustomization owns the CNPG Cluster's PVCs; pruning on deletion
        # would take the console's approval ledger with them.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        # Nothing downstream reconciles until CNPG accepts connections.
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            # The Cluster CRD and CNPG's failurePolicy: Fail webhook.
            cnpg,
            external_secrets_operator,
            # The ServiceMonitor CRD.
            monitoring_crds,
        ),
    )
