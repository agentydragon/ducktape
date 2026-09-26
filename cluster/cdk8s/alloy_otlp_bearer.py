"""Mirrors the rotator-published alloy-otlp bearer into every agent sandbox namespace
(cluster/k8s/agents/alloy-otlp-bearer), where the session OTLP forwarder
(devinfra/claude/ensure_otel_forwarder.sh) reads it to authenticate Claude Code telemetry
relayed to alloy-otlp.allegedly.works. Same pattern as haku-mail-token (haku/mailbox.py): the
source is the flux-system Secret the authentik-jwt-rotation CronJob writes via its k8s_secret
output. The SOPS-encrypted Secret beside the output stays hand-written.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ClusterExternalSecret, cluster_remote_data

NAME = "alloy-otlp-bearer"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/alloy-otlp-bearer"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ClusterExternalSecret(
        chart,
        NAME,
        name=NAME,
        # Least privilege: only namespaces whose sessions run the OTLP forwarder.
        namespaces=["claude-sandbox", "haku-sandbox"],
        store_name="kubernetes-flux-system-secret-store",
        refresh="1m",
        data=[cluster_remote_data(NAME, "token")],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.sops.yaml", f"{NAME}.k8s.yaml"]),
    )


def alloy_otlp_bearer(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    claude_rbac: Kustomization,
    haku_rbac: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        retry_interval=None,
        wait=None,
        depends_on=flux_kustomization_depends_on_many(
            # ClusterSecretStore + CRDs
            external_secrets_config,
            # claude-sandbox namespace
            claude_rbac,
            # haku-sandbox namespace
            haku_rbac,
        ),
        timeout="2m",
        decryption=SOPS_DECRYPTION,
    )
