"""Flux Kustomizations for the cluster/k8s/haku slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def haku_console_namespace() -> dict[str, object]:
    name = "haku-console-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/haku/console-namespace",
            prune=True,
            # The ducktape-ci pull credential lives here rather than beside the console's own
            # workloads, because haku-console-migration needs it and the console layer depends on
            # that migration — putting the ExternalSecret in the console layer makes the pull
            # secret wait on the Job that needs it.
            depends_on=[KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux")],
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def haku_forgejo_tea() -> dict[str, object]:
    name = "haku-forgejo-tea"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(
                    name="haku-rbac",  # haku-sandbox ns the secret lives in
                    namespace="ducktape-flux",
                )
            ],
        ),
        description=(
            "Forgejo API token Reflector mirrors into haku-ci's KEDA scaler. "
            "Split out of haku/managed-agent so haku-ci doesn't depend on the "
            "(parked) worker."
        ),
    )


def haku_mailbox_namespace() -> dict[str, object]:
    name = "haku-mailbox-namespace"
    return flux_kustomization(
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


def haku_mailbox() -> dict[str, object]:
    name = "haku-mailbox"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-mailbox-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-mailbox-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="cert-manager",  # Certificate CRD + controller
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="agent-machine-access-tf",  # Authentik provider + Haku mailbox identity
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="gateway",  # HTTPRoute parent for the JMAP/management API
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="external-secrets-config",  # ClusterSecretStore + CRDs (token mirror)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="cert-manager-issuer-config",  # ${LETSENCRYPT_ISSUER}
                    namespace="ducktape-flux",
                ),
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


def haku_mailbox_db() -> dict[str, object]:
    name = "haku-mailbox-db"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="haku-mailbox-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def haku_namespace() -> dict[str, object]:
    name = "haku-namespace"
    return flux_kustomization(
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


def haku_rbac() -> dict[str, object]:
    name = "haku-rbac"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/haku/rbac",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[KustomizationSpecDependsOn(name="haku-namespace", namespace="ducktape-flux")],
        ),
    )


def haku_ui_image_webhook() -> dict[str, object]:
    name = "haku-ui-image-webhook"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="haku-state", namespace="ducktape-flux")
            ],
        ),
    )


def haku_workloads() -> dict[str, object]:
    name = "haku-workloads"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="haku-state", namespace="ducktape-flux")
            ],
        ),
    )


def haku_workspaces() -> dict[str, object]:
    name = "haku-workspaces"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(
                    name="agent-sandbox-controller",  # shared CRDs + controller
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="haku-rbac",  # haku-sandbox ns + haku-sandbox-admin Role the SA rolebinding needs
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="haku-egress-proxy",  # the fence haku-sandbox is opted into
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="kyverno-policies",  # cleanup-controller ClusterRole the janitor needs
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="external-secrets-config",  # ClusterSecretStore + CRDs for the ESO
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="forgejo-images",  # mints the source forgejo-images-creds the ESO reflects
                    namespace="ducktape-flux",
                ),
            ],
        ),
        description="General Haku workspaces in haku-sandbox.",
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/haku/console-namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_console_namespace())
    path = root / "cluster/k8s/haku/forgejo-tea/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_forgejo_tea())
    path = root / "cluster/k8s/haku/mailbox-namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_mailbox_namespace())
    path = root / "cluster/k8s/haku/mailbox/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_mailbox())
    path = root / "cluster/k8s/haku/mailbox/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_mailbox_db())
    path = root / "cluster/k8s/haku/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_namespace())
    path = root / "cluster/k8s/haku/rbac/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_rbac())
    path = root / "cluster/k8s/haku/ui-image-webhook/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_ui_image_webhook())
    path = root / "cluster/k8s/haku/workloads/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_workloads())
    path = root / "cluster/k8s/haku/workspaces/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_workspaces())
