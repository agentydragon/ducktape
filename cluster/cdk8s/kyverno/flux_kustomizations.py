"""Flux Kustomizations for the cluster/k8s/kyverno slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def kyverno(chart: Chart) -> Kustomization:
    name = "kyverno"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="10m0s",
            wait=True,
            # No dependsOn: kyverno manages its own TLS via internal certmanager-controller
            # (certManager.enabled: false in kyverno.yaml). No cert-manager dependency.
            #
            # Health check strategy — all three must pass before downstream dependsOn fires:
            #
            # The critical gate is the VWC check. With internal cert management, kyverno's
            # webhook-controller creates the VWC dynamically ONLY AFTER the TLS server is
            # ready (see kyverno.yaml comments for source code references). So VWC
            # existence is a true readiness signal — it means the webhook can serve HTTPS.
            #
            # Note: Flux/kstatus treats VWC as "unknown" status = pass (flux2#4346), so
            # this check only verifies existence, not deep health. But with internal certs
            # that's sufficient — the VWC won't exist until the webhook is operational.
            # With certManager.enabled=true, this check would be useless because the Helm
            # chart doesn't template the VWC (it's always dynamic), but the Deployment
            # readiness probe would pass before the VWC is created, creating a race.
            #
            # wait: true is critical — without it, dependsOn only waits for YAML to be
            # applied, not for resources to be healthy.
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="kyverno", namespace="flux-system"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apiextensions.k8s.io/v1",
                    kind="CustomResourceDefinition",
                    name="clusterpolicies.kyverno.io",
                    namespace="",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="kyverno-admission-controller", namespace="kyverno"
                ),
                KustomizationSpecHealthChecks(
                    api_version="admissionregistration.k8s.io/v1",
                    kind="ValidatingWebhookConfiguration",
                    name="kyverno-resource-validating-webhook-cfg",
                    namespace="",
                ),
            ],
        ),
    )


def kyverno_policies(chart: Chart, kyverno: Kustomization) -> Kustomization:
    name = "kyverno-policies"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            depends_on=[
                # Policies require Kyverno CRDs to be installed
                flux_kustomization_depends_on(kyverno)
            ],
            interval="5m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            timeout="2m",
        ),
    )
