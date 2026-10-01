"""Flux Kustomizations for the cluster/parked slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import PARKED_ROOT


def agent_box(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    kubevirt: Kustomization,
    external_secrets_operator: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    name = "agent-box"
    return flux_kustomization(
        chart,
        name,
        artifact,
        annotations={"ducktape.org/parked": "true"},
        # Keep this controller inactive while the unschedulable legacy VM is retired.
        # Its VM and local disk remain untouched until explicitly deleted.
        suspend=True,
        timeout="30m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            kubevirt,
            external_secrets_operator,
            # Kyverno's failurePolicy: Fail webhooks admit the Namespace.
            kyverno,
        ),
    )


def buildbuddy_executor(chart: Chart) -> Kustomization:
    name = "buildbuddy-executor"
    return flux_kustomization(
        chart,
        name,
        # Not flux-system's source: its sparse checkout (gotk-sync.yaml) omits the parked tree.
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace="ducktape-flux"
        ),
        annotations={"ducktape.org/parked": "true"},
        suspend=True,
        timeout="5m",
        path=f"./{PARKED_ROOT}/buildbuddy-executor",
        decryption=SOPS_DECRYPTION,
    )


def haku_cloud_agent(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    tofu_controller: Kustomization,
) -> Kustomization:
    name = "haku-cloud-agent"
    return flux_kustomization(
        chart,
        name,
        # Decrypt the SOPS workspace API key in this dir;
        # without this Flux applies the raw ENC[...] ciphertext and the runner gets a
        # bogus key/token (401).
        artifact,
        annotations={"ducktape.org/parked": "true"},
        suspend=True,
        timeout="10m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, tofu_controller),
    )


def docker_ci(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    claude_rbac: Kustomization,
    cert_manager: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    name = "docker-ci"
    return flux_kustomization(
        chart,
        name,
        artifact,
        annotations={"ducktape.org/parked": "true"},
        suspend=True,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            claude_rbac,
            # The Certificate CRD and cert-manager's failurePolicy: Fail webhook.
            cert_manager,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment and Namespace.
            kyverno,
        ),
    )


def gecko(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    gecko_namespace: Kustomization,
    kubevirt: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "gecko"
    return flux_kustomization(
        chart,
        name,
        artifact,
        annotations={"ducktape.org/parked": "true"},
        # Keep this controller inactive while the unschedulable legacy VM is retired.
        # Its VM and local disk remain untouched until explicitly deleted.
        suspend=True,
        timeout="30m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(gecko_namespace, kubevirt, external_secrets_operator),
    )


def gecko_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "gecko-namespace"
    return flux_kustomization(
        chart,
        name,
        artifact,
        annotations={"ducktape.org/parked": "true"},
        # The retired VM stack and its namespace were intentionally deleted.
        # Keep this controller paused so Flux does not recreate the empty namespace.
        suspend=True,
        timeout="2m",
    )


def haku_dispatch(chart: Chart, cnpg: Kustomization, external_secrets_operator: Kustomization) -> Kustomization:
    name = "haku-dispatch"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace="ducktape-flux"
        ),
        annotations={"ducktape.org/parked": "true"},
        # Haku dispatch is intentionally parked. Flip this to false only when the
        # worker-zone and provider wiring has been deliberately restored.
        suspend=True,
        timeout="10m",
        path="./haku/x/dispatch/deploy",
        deletion_policy=KustomizationSpecDeletionPolicy.WAIT_FOR_TERMINATION,
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator),
    )


def haku_managed_agent(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    haku_namespace: Kustomization,
    haku_rbac: Kustomization,
    haku_egress_proxy: Kustomization,
) -> Kustomization:
    name = "haku-managed-agent"
    return flux_kustomization(
        chart,
        name,
        artifact,
        annotations={"ducktape.org/parked": "true"},
        suspend=True,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            # ExternalSecret CRD and ESO's failurePolicy: Fail webhook
            external_secrets_operator,
            haku_namespace,
            haku_rbac,
            # destructive-if-out-of-order: the haku-sandbox egress fence must precede sandbox pods.
            haku_egress_proxy,
        ),
    )


def sdr(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, external_secrets_operator: Kustomization
) -> Kustomization:
    name = "sdr"
    return flux_kustomization(
        chart,
        name,
        artifact,
        annotations={"ducktape.org/parked": "true"},
        # Temporarily disabled until the radio is set up again after relocation.
        suspend=True,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator),
    )
