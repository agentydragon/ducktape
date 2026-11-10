"""The plaid-mcp Flux component and its owned manifest directory."""

from functools import partial
from pathlib import Path

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import generation
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.plaid_mcp import app, db, pgweb, spend

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp"
_NAMESPACE = db.NAMESPACE


def write_directory(root: Path, artifact: ArtifactGeneratorSpecArtifacts) -> RenderedDirectory:
    return generation.write_directory(
        root,
        artifact,
        partial(generation.namespace_chart, name=_NAMESPACE, vpa=Vpa.DISABLED),
        app.chart,
        db.chart,
        pgweb.chart,
        spend.chart,
        siblings=["plaid-client-credentials.sops.yaml"],
        components=["image-pins"],
        config_map_generator=[
            ConfigMapArgs(name="plaid-mcp-db-readonly-sql", namespace=_NAMESPACE, files=["readonly-role.sql"])
        ],
    )


def plaid_mcp(
    chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    authentik: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "plaid-mcp"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="10m",
        # Web OIDC credentials are supplied by an ExternalSecret and required by
        # the pods at startup; wait=True tracks their readiness. Unrelated
        # Authentik Terraform projects must not block app image/config updates.
        # Finance spend policy reconciles independently; its Secret copy and
        # workload become ready once that configuration arrives.
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator, authentik, seaweedfs_operator),
    )
