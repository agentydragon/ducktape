"""Flux Kustomizations for the cluster/k8s/haku slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def haku_console_namespace(chart: Chart, external_secrets_config: Kustomization) -> Kustomization:
    name = "haku-console-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/haku/console-namespace",
            prune=True,
            # The ducktape-ci pull credential lives here rather than beside the console's own
            # workloads, because haku-console-migration needs it and the console layer depends on
            # that migration — putting the ExternalSecret in the console layer makes the pull
            # secret wait on the Job that needs it.
            depends_on=[flux_kustomization_depends_on(external_secrets_config)],
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
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
            path="./cluster/k8s/haku/forgejo-tea",
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


def haku_mailbox_namespace(chart: Chart) -> Kustomization:
    name = "haku-mailbox-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/haku/mailbox-namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def haku_mailbox(
    chart: Chart,
    forgejo_images: Kustomization,
    haku_mailbox_namespace: Kustomization,
    haku_mailbox_db: Kustomization,
    cert_manager: Kustomization,
    agent_machine_access_tf: Kustomization,
    gateway: Kustomization,
    external_secrets_config: Kustomization,
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
            path="./cluster/k8s/haku/mailbox/app",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="haku-mailbox-app",
                namespace="ducktape-flux",
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                flux_kustomization_depends_on(forgejo_images),
                flux_kustomization_depends_on(haku_mailbox_namespace),
                flux_kustomization_depends_on(haku_mailbox_db),
                # Certificate CRD + controller
                flux_kustomization_depends_on(cert_manager),
                # Authentik provider + Haku mailbox identity
                flux_kustomization_depends_on(agent_machine_access_tf),
                # HTTPRoute parent for the JMAP/management API
                flux_kustomization_depends_on(gateway),
                # ClusterSecretStore + CRDs (token mirror)
                flux_kustomization_depends_on(external_secrets_config),
                # ${LETSENCRYPT_ISSUER}
                flux_kustomization_depends_on(cert_manager_issuer_config),
            ],
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),  # ${LETSENCRYPT_ISSUER}
        ),
    )


def haku_mailbox_db(
    chart: Chart, haku_mailbox_namespace: Kustomization, cnpg: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "haku-mailbox-db"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/haku/mailbox/db",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                flux_kustomization_depends_on(haku_mailbox_namespace),
                flux_kustomization_depends_on(cnpg),
                flux_kustomization_depends_on(local_path_provisioner),
            ],
        ),
    )


def haku_namespace(chart: Chart) -> Kustomization:
    name = "haku-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/haku/namespace",
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
            path="./cluster/k8s/haku/rbac",
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
            path="./cluster/k8s/haku/ui-image-webhook",
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
            path="./cluster/k8s/haku/workloads",
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
            path="./cluster/k8s/haku/workspaces/app",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="haku-workspaces-app",
                namespace="ducktape-flux",
            ),
            depends_on=[
                # shared CRDs + controller
                flux_kustomization_depends_on(agent_sandbox_controller),
                # haku-sandbox ns + haku-sandbox-admin Role the SA rolebinding needs
                flux_kustomization_depends_on(haku_rbac),
                # the fence haku-sandbox is opted into
                flux_kustomization_depends_on(haku_egress_proxy),
                # cleanup-controller ClusterRole the janitor needs
                flux_kustomization_depends_on(kyverno_policies),
                # ClusterSecretStore + CRDs for the ESO
                flux_kustomization_depends_on(external_secrets_config),
                # mints the source forgejo-images-creds the ESO reflects
                flux_kustomization_depends_on(forgejo_images),
            ],
        ),
        description="General Haku workspaces in haku-sandbox.",
    )
