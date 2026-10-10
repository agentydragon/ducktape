"""The AT&T BGW320 gateway's device access code, which unlocks its login-only pages (event log,
NAT session table). The SOPS Secret stays hand-written and only Flux decrypts it; this builds the
directory's Flux Kustomization."""

from __future__ import annotations

from cdk8s import Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "att-gateway-exporter-secrets"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"


def att_gateway_exporter_secrets(
    flux_chart: Chart, directory: RenderedDirectory, monitoring_namespace: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(monitoring_namespace)],
        description="AT&T gateway device access code, for the exporter's login-only pages.",
    )
