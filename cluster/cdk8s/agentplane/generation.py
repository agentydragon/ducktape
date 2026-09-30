"""Write staging and testing Agentplane environment manifests."""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.flux import health_checks as flux_health_checks, kustomize_kustomization
from cluster.cdk8s.generation import write_app, write_yaml

_HEALTH_CHECK_KINDS = ("Namespace", "Cluster", "Database", "Deployment", "Certificate", "Bundle")


def environment_health_checks(chart: Chart, namespace: str) -> list[KustomizationSpecHealthChecks]:
    """Derive Flux health-check values from an Agentplane environment chart."""
    checks = flux_health_checks(chart, _HEALTH_CHECK_KINDS)
    # trust-manager writes a Bundle's target ConfigMap, named after the Bundle, asynchronously
    # and outside the artifact, so the Kustomization checks it explicitly.
    return [
        *checks,
        *(
            KustomizationSpecHealthChecks(api_version="v1", kind="ConfigMap", name=check.name, namespace=namespace)
            for check in checks
            if check.kind == "Bundle"
        ),
    ]


def write_environment_manifests(
    root: Path, env: Environment, build: Callable[[App], Chart], *more_charts: Callable[[App], Chart]
) -> Chart:
    """Synthesize the environment's chart, then `more_charts`, into `env.output_dir`'s one
    generated file. Single failure domain by design -- including the CNPG Postgres
    `Cluster` -- accepted for both non-production environments.

    Writes the root Kustomization: the generated file, the environment's `extra_resources`
    and its `image_pins` Component. Returns the environment's chart so the per-environment
    Flux factory can build health checks from these same objects.
    """
    app = App()
    chart = build(app)
    for build_more in more_charts:
        build_more(app)
    write_yaml(
        root / env.output_dir / "kustomization.yaml",
        kustomize_kustomization(
            resources=[write_app(root, env.output_dir, app), *env.extra_resources],
            components=[posixpath.relpath(env.image_pins, env.output_dir)],
        ),
    )
    return chart
