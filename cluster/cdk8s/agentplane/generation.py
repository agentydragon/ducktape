"""Write staging and testing Agentplane environment manifests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.flux import NAMESPACE, flux_kustomization, health_checks, kustomize_kustomization
from cluster.cdk8s.generation import CNPG_DATABASE_READY, sops_decryption, write_yaml

_HEALTH_CHECK_KINDS = ("Namespace", "Cluster", "Database", "Deployment", "Certificate", "Bundle")


def _health_checks(chart: Chart, namespace: str) -> list[KustomizationSpecHealthChecks]:
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


def write_environment_manifests(root: Path, env: Environment, build: Callable[[App], Chart]) -> None:
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
    chart = build(app)
    app.synth()

    write_yaml(
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
                health_checks=_health_checks(chart, env.namespace),
                health_check_exprs=[
                    KustomizationSpecHealthCheckExprs(
                        api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
                    )
                ],
                decryption=sops_decryption(env.extra_resources),
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=env.namespace, namespace=NAMESPACE
                ),
                depends_on=[KustomizationSpecDependsOn(name=dep) for dep in env.depends_on],
            ),
        ),
    )
    write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(resources=["agentplane.k8s.yaml", *env.extra_resources], components=["./image-pins"]),
    )
