"""Mirrors the rotator-published alloy-otlp bearer into every agent sandbox namespace
(cluster/k8s/agents/alloy-otlp-bearer), where the session OTLP forwarder
(devinfra/claude/ensure_otel_forwarder.sh) reads it to authenticate Claude Code telemetry
relayed to alloy-otlp.allegedly.works. Same pattern as haku-mail-token (haku/mailbox.py): the
source is the flux-system Secret the authentik-jwt-rotation CronJob writes via its k8s_secret
output. The SOPS-encrypted Secret beside the output stays hand-written.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import (
    ClusterExternalSecret,
    ClusterSecretStoreRef,
    cluster_remote_data,
)

NAME = "alloy-otlp-bearer"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/alloy-otlp-bearer"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ClusterExternalSecret(
        chart,
        NAME,
        metadata=ApiObjectMetadata(name=NAME),
        # Least privilege: only namespaces whose sessions run the OTLP forwarder.
        namespaces=["claude-sandbox", "haku-sandbox"],
        secret_store_ref=ClusterSecretStoreRef.cluster("kubernetes-flux-system-secret-store"),
        refresh_interval="1m",
        data=[cluster_remote_data(NAME, "token")],
    )
    return chart


def alloy_otlp_bearer(
    chart: Chart,
    directory: RenderedDirectory,
    external_secrets_operator: Kustomization,
    claude_rbac: Kustomization,
    haku_rbac: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        retry_interval=None,
        wait=None,
        depends_on=flux_kustomization_depends_on_many(
            # ExternalSecret CRD and ESO's failurePolicy: Fail webhook
            external_secrets_operator,
            # claude-sandbox namespace
            claude_rbac,
            # haku-sandbox namespace
            haku_rbac,
        ),
        timeout="2m",
    )
