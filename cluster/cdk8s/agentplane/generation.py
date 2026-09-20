"""Write staging and testing Agentplane environment manifests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.flux import health_checks, kustomize_kustomization
from cluster.cdk8s.generation import write_yaml

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


def write_environment_manifests(root: Path, env: Environment, build: Callable[[App], Chart]) -> Chart:
    """Synthesize the environment's chart into `cluster/k8s/<namespace>` as a single
    `agentplane.k8s.yaml`. Single failure domain by design -- including the CNPG Postgres
    `Cluster` -- accepted for both non-production environments.

    Also (re)writes its root Kustomization: the one generated file plus the environment's
    hand-written `extra_resources`. The sibling image-pins/ Component stays hand-written,
    same as litellm/ha-mcp. Returns the chart so the per-environment Flux factory can build
    health checks from these same objects.
    """
    env_dir = f"cluster/k8s/{env.namespace}"
    out_dir = root / env_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = build(app)
    app.synth()

    write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(resources=["agentplane.k8s.yaml", *env.extra_resources], components=["./image-pins"]),
    )
    return chart
