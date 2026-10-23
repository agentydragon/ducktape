"""Write staging and testing Agentplane environment manifests."""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s import flux
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_app, write_yaml

_HEALTH_CHECK_KINDS = ("Namespace", "Cluster", "Database", "Deployment", "Certificate", "Bundle", "Job")


@dataclass(frozen=True)
class ManifestLayout:
    """Where a deployed environment's generated manifests go and how Flux applies them."""

    # The Flux Kustomization's description.
    flux_description: str
    # The root Kustomization's directory and the hand-written image-pins Component it
    # includes, repo-relative (manifest_roots.py).
    output_dir: str
    image_pins: str
    delivery_output_dir: str
    # Files the root Kustomization lists beside the generated one.
    extra_resources: Sequence[str]


def environment_health_checks(chart: Chart, namespace: str) -> list[KustomizationSpecHealthChecks]:
    """Derive Flux health-check values from an Agentplane environment chart."""
    checks = flux.health_checks(chart, _HEALTH_CHECK_KINDS)
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
    root: Path, layout: ManifestLayout, build: Callable[[App], Chart], *more_charts: Callable[[App], Chart]
) -> Chart:
    """Synthesize the environment's chart, then `more_charts`, into `layout.output_dir`'s one
    generated file. Single failure domain by design -- including the CNPG Postgres
    `Cluster` -- accepted for both non-production environments.

    Writes the root Kustomization: the generated file, the layout's `extra_resources`
    and its `image_pins` Component. Returns the environment's chart so the per-environment
    Flux factory can build health checks from these same objects.
    """
    app = App()
    chart = build(app)
    for build_more in more_charts:
        build_more(app)
    write_yaml(
        root / layout.output_dir / "kustomization.yaml",
        kustomize_kustomization(
            resources=[
                write_app(
                    root,
                    layout.output_dir,
                    app,
                    # The interception proxy terminates TLS for the namespace; its allowlist is the
                    # egress PolicySets, not SNI on its own egress rule.
                    unpinned_https_egress={egress.NAME.removeprefix("agentplane-")},
                ),
                *layout.extra_resources,
            ],
            components=[posixpath.relpath(layout.image_pins, layout.output_dir)],
        ),
    )
    return chart
