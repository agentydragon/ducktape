"""Flux Kustomizations for the cluster/k8s/haku slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def haku_forgejo_tea(chart: Chart, haku_rbac: Kustomization) -> Kustomization:
    name = "haku-forgejo-tea"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                # haku-sandbox ns the secret lives in
                flux_kustomization_depends_on(haku_rbac)
            ],
        ),
        description=(
            "Forgejo API token Reflector mirrors into haku-ci's KEDA scaler. "
            "Split out of haku/managed-agent so haku-ci doesn't depend on the "
            "(parked) worker."
        ),
    )


def haku_mailbox(
    chart: Chart,
    cnpg: Kustomization,
    cert_manager: Kustomization,
    external_secrets_operator: Kustomization,
    cert_manager_issuer_config: Kustomization,
) -> Kustomization:
    name = "haku-mailbox"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(
                cnpg,
                # Certificate CRD + controller
                cert_manager,
                # ExternalSecret and ClusterExternalSecret CRDs and webhooks
                external_secrets_operator,
                # ${LETSENCRYPT_ISSUER}
                cert_manager_issuer_config,
            ),
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),  # ${LETSENCRYPT_ISSUER}
        ),
    )


def haku_namespace(chart: Chart) -> Kustomization:
    name = "haku-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def haku_rbac(chart: Chart, haku_namespace: Kustomization) -> Kustomization:
    name = "haku-rbac"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[flux_kustomization_depends_on(haku_namespace)],
        ),
    )


def haku_ui_image_webhook(chart: Chart, haku_state: Kustomization) -> Kustomization:
    name = "haku-ui-image-webhook"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                # haku-state provisions the forgejo-webhook-token Secret (the Receiver's secretRef)
                # and the Forgejo package webhook that targets this receiver.
                flux_kustomization_depends_on(haku_state)
            ],
        ),
    )


def haku_workloads(chart: Chart, haku_state: Kustomization) -> Kustomization:
    name = "haku-workloads"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./",
            prune=True,
            # Don't gate on the inner haku-state-workloads Kustomization's readiness — it's
            # NotReady until Haku first seeds k8s/, which would otherwise wedge this wrapper.
            wait=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                # The forgejo/haku-state Terraform apply provisions the haku-state repo and the
                # haku-forgejo-git Secret (now also reflected into flux-system for the
                # GitRepository's basic auth).
                flux_kustomization_depends_on(haku_state)
            ],
        ),
    )


def haku_workspaces(
    chart: Chart,
    agent_sandbox_controller: Kustomization,
    haku_rbac: Kustomization,
    haku_egress_proxy: Kustomization,
    kyverno_policies: Kustomization,
    external_secrets_config: Kustomization,
    external_creds: Kustomization,
    forgejo_images: Kustomization,
) -> Kustomization:
    name = "haku-workspaces"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            path="./",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="haku-workspaces-app",
                namespace="ducktape-flux",
            ),
            depends_on=flux_kustomization_depends_on_many(
                # shared CRDs + controller
                agent_sandbox_controller,
                # haku-sandbox ns + haku-sandbox-admin Role the SA rolebinding needs
                haku_rbac,
                # the fence haku-sandbox is opted into
                haku_egress_proxy,
                # cleanup-controller ClusterRole the janitor needs
                kyverno_policies,
                # Source Secret grants + ClusterSecretStore for the ESO
                external_creds,
                external_secrets_config,
                # mints the source forgejo-images-creds the ESO reflects
                forgejo_images,
            ),
        ),
        description="General Haku workspaces in haku-sandbox.",
    )
