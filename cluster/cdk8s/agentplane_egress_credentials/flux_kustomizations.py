"""Flux Kustomizations for the cluster/k8s/agentplane-egress-credentials slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def agentplane_egress_credentials_namespace(chart: Chart) -> Kustomization:
    name = "agentplane-egress-credentials-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            timeout="2m",
            path="./cluster/k8s/agentplane-egress-credentials/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
        description="The namespace holding the credentials the Agentplane egress proxy substitutes.",
    )


def agentplane_egress_credentials(
    chart: Chart,
    agentplane_egress_credentials_namespace: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    name = "agentplane-egress-credentials"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            path="./cluster/k8s/agentplane-egress-credentials/secrets",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(
                agentplane_egress_credentials_namespace,
                # the source-side grant on the agentydragon-agent PAT
                external_creds,
                # the ClusterSecretStore the PAT is read through
                external_secrets_config,
            ),
        ),
        description=(
            "The credentials the Agentplane egress proxy substitutes (the "
            "agentydragon-agent GitHub PAT via ESO) and the proxy "
            "ServiceAccount's read grant on them."
        ),
    )
