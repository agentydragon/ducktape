"""Write staging and testing Agentplane environment manifests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.flux import health_checks as flux_health_checks, kustomize_kustomization
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

_HEALTH_CHECK_KINDS = ("Namespace", "Cluster", "Database", "Deployment", "Certificate", "Bundle")


def environment_health_checks(chart: Chart, namespace: str) -> list[KustomizationSpecHealthChecks]:
    """Derive Flux health-check values from an Agentplane environment chart."""
    checks = flux_health_checks(chart, _HEALTH_CHECK_KINDS)
    # trust-manager names a Bundle's target ConfigMap after the Bundle.
    return [
        *checks,
        *(
            KustomizationSpecHealthChecks(api_version="v1", kind="ConfigMap", name=check.name, namespace=namespace)
            for check in checks
            if check.kind == "Bundle"
        ),
    ]


def output_dir(env: Environment) -> str:
    return f"{HAND_WRITTEN_ROOT}/{env.namespace}"


def write_environment_manifests(
    root: Path, env: Environment, build: Callable[[App], Chart], *, write_kustomization: bool = True
) -> Chart:
    """Synthesize the environment's chart into `output_dir(env)` as a single
    `agentplane.k8s.yaml`. Single failure domain by design -- including the CNPG Postgres
    `Cluster` -- accepted for both non-production environments.

    Optionally rewrites the root Kustomization: the generated file plus the environment's
    hand-written `extra_resources`. Agentplane testing keeps its root Kustomization
    hand-written because Flux image automation updates its inline `images:` tags. Returns
    the chart so the per-environment Flux factory can build health checks from these same
    objects.
    """
    out_dir = root / output_dir(env)
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = build(app)
    app.synth()

    if write_kustomization:
        write_yaml(
            out_dir / "kustomization.yaml",
            kustomize_kustomization(
                resources=["agentplane.k8s.yaml", *env.extra_resources], components=["./image-pins"]
            ),
        )
    return chart
