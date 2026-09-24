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
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption, write_yaml
from cluster.cdk8s.haku import console
from cluster.cdk8s.haku.console import Console
from cluster.cdk8s.haku.database import Db
from cluster.cdk8s.haku.kube_api_proxy import KubeApiProxy
from cluster.cdk8s.haku.migration import Migration
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = console.NAME
NAMESPACE = console.NAMESPACE
PATH = f"{HAND_WRITTEN_ROOT}/haku/console"
# Long enough for the slowest cold path -- CNPG bootstrapping a fresh two-instance Cluster,
# then the migration and the GRANTs converging on their retries behind it.
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

CONFIG_MAP_GENERATOR = (
    ConfigMapArgs(name=console.INDEXER_SQL_CONFIG_MAP, namespace=console.NAMESPACE, files=["indexer-role.sql"]),
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
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "name": NAMESPACE,
            },
        ),
    )
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    Db(chart, "db")
    Migration(chart, "migration")
    Console(chart, "console")
    KubeApiProxy(chart, "kube-api-proxy")
    add_fleet_rules(chart)
    return chart


def write_console_manifests(root: Path) -> None:
    """Synthesize the console resource chart and its directory Kustomize config."""
    out_dir = root / PATH
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    console_chart(app)
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


def haku_console(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    local_path_provisioner: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    agent_machine_access_tf: Kustomization,
    reflector: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    """Build the Flux graph node from its predecessor nodes."""
    return flux_kustomization(
        flux_chart,
        NAME,
        artifact,
        timeout=TIMEOUT,
        # This one Kustomization owns the CNPG Cluster's PVCs; pruning on deletion
        # would take the console's approval ledger with them.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=sops_decryption(EXTRA_RESOURCES),
        # The two Jobs gate every dependent Kustomization: nothing downstream
        # reconciles until the schema is migrated and the indexer GRANTs applied.
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
            # Copies aiquota's bearer, the ActivityWatch read token and the egress proxy's CA
            # into this namespace.
            reflector,
            external_creds,
            external_secrets_config,
            # The ServiceMonitor CRD.
            monitoring_crds,
        ),
    )
