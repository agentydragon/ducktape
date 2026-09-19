"""Flux Kustomizations for the cluster/k8s/parked slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def agent_box() -> dict[str, object]:
    name = "agent-box"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Keep this controller inactive while the unschedulable legacy VM is retired.
            # Its VM and local disk remain untouched until explicitly deleted.
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="30m",
            path="./cluster/k8s/parked/agent-box",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="kubevirt", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cdi", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-operator", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-public-s3", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="agent-box"),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="agent-box-vm-images-s3-reader",
                    namespace="agent-box",
                ),
                KustomizationSpecHealthChecks(
                    api_version="cdi.kubevirt.io/v1beta1",
                    kind="DataVolume",
                    name="agent-box-root",
                    namespace="agent-box",
                ),
                KustomizationSpecHealthChecks(
                    api_version="kubevirt.io/v1", kind="VirtualMachine", name="agent-box", namespace="agent-box"
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def archivebox() -> dict[str, object]:
    name = "archivebox"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Retain the declaration without allowing Flux to recreate retired objects.
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="15m",
            path="./cluster/k8s/parked/archivebox",
            prune=True,
            # Captured web archives are user data. Removing this experimental controller
            # must not implicitly delete the Deployment, namespace, or either PVC.
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-csi", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="archivebox"),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="archivebox", namespace="archivebox"
                ),
            ],
        ),
        description=(
            "Suspended ArchiveBox experiment; former Authentik header SSO and split local/SeaweedFS CSI storage."
        ),
    )
    manifest["metadata"] = {
        "name": name,
        "namespace": "ducktape-flux",
        "annotations": {
            "description": (
                "Suspended ArchiveBox experiment; former Authentik header SSO and split local/SeaweedFS CSI storage."
            ),
            "ducktape.org/parked": "true",
        },
    }
    return manifest


def augur_evidence() -> dict[str, object]:
    name = "augur-evidence"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/parked/augur-evidence",
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the Forgejo repo + service users + the
            # augur-evidence-git-{write,read} Secrets) so the scraper/git-sync can depend on it.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="augur-evidence",
                    namespace="flux-system",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo API must be up (provider target)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="budget-namespace",  # the git-creds Secrets land in the budget namespace
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def authelia() -> dict[str, object]:
    name = "authelia"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/parked/authelia",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="authelia", namespace="authelia"
                )
            ],
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def browsertrix() -> dict[str, object]:
    name = "browsertrix"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="25m",
            path="./cluster/k8s/parked/browsertrix/app",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="browsertrix-app", namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="browsertrix-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-browsertrix-bucket", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-secrets", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-csi", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="browsertrix",
                    namespace="browsertrix",
                )
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def seaweedfs_browsertrix_bucket() -> dict[str, object]:
    # Retain the existing resource name so moving its source path does not delete
    # the live Bucket before its explicit retirement.
    name = "seaweedfs-browsertrix-bucket"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/parked/browsertrix/bucket",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="browsertrix-bucket",
                namespace="ducktape-flux",
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-secrets", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="browsertrix", namespace="seaweedfs"
                )
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def browsertrix_namespace() -> dict[str, object]:
    name = "browsertrix-namespace"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="1m",
            path="./cluster/k8s/parked/browsertrix/namespace",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def browsertrix_retained() -> dict[str, object]:
    name = "browsertrix-retained"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/parked/browsertrix/retained",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def budget() -> dict[str, object]:
    name = "budget"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/parked/budget",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(
                    name="budget-ledger",  # provisions budget-ledger-git-creds in the budget ns
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def buildbuddy_executor() -> dict[str, object]:
    name = "buildbuddy-executor"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="flux-system", namespace="flux-system"
            ),
            path="./cluster/k8s/parked/buildbuddy-executor",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="buildbuddy-executor",
                    namespace="buildbuddy-executor",
                )
            ],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def haku_cloud_agent() -> dict[str, object]:
    name = "haku-cloud-agent"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/parked/cloud-agent-tf",
            prune=True,
            wait=True,
            # Decrypt the SOPS Secrets in this dir (anthropic-api-key, haku-kube-token);
            # without this Flux applies the raw ENC[...] ciphertext and the runner gets a
            # bogus key/token (401).
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="haku-cloud-agent",
                    namespace="flux-system",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-creds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                # The agent reaches the cluster through this MCP; its first deployment run
                # needs it serving.
                KustomizationSpecDependsOn(name="kubectl-machine-mcp", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def docker_ci() -> dict[str, object]:
    name = "docker-ci"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/docker-ci",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="docker-ci", namespace="docker-ci"
                )
            ],
            depends_on=[
                # No storage dep (emptyDir, not a CSI PVC). Needs the cluster-internal-ca
                # ClusterIssuer for the mTLS Certificates and agent-rbac-base for the
                # claude-sandbox namespace the client Certificate lives in.
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="claude-rbac", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def egress_proxy_rugged() -> dict[str, object]:
    name = "egress-proxy-rugged"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Decommissioned by operator request; keep its configuration but stop reconciliation.
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/egress-proxy-rugged",
            prune=True,
            wait=True,
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def firecrawl() -> dict[str, object]:
    name = "firecrawl"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/firecrawl/app",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="firecrawl-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="firecrawl-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def firecrawl_db() -> dict[str, object]:
    name = "firecrawl-db"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/firecrawl/db",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="firecrawl-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def firecrawl_namespace() -> dict[str, object]:
    name = "firecrawl-namespace"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="1h",
            path="./cluster/k8s/parked/firecrawl/namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="1m",
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def gecko() -> dict[str, object]:
    name = "gecko"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Keep this controller inactive while the unschedulable legacy VM is retired.
            # Its VM and local disk remain untouched until explicitly deleted.
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="30m",
            path="./cluster/k8s/parked/gecko/app",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="gecko-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="kubevirt", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cdi", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-operator", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-public-s3", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="gecko-vm-images-s3-reader",
                    namespace="gecko",
                ),
                KustomizationSpecHealthChecks(
                    api_version="cdi.kubevirt.io/v1beta1", kind="DataVolume", name="gecko-root", namespace="gecko"
                ),
                KustomizationSpecHealthChecks(
                    api_version="kubevirt.io/v1", kind="VirtualMachine", name="gecko", namespace="gecko"
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def gecko_namespace() -> dict[str, object]:
    name = "gecko-namespace"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # The retired VM stack and its namespace were intentionally deleted.
            # Keep this controller paused so Flux does not recreate the empty namespace.
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="2m",
            path="./cluster/k8s/parked/gecko/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="gecko")],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def google_workspace_mcp() -> dict[str, object]:
    name = "google-workspace-mcp"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/google-workspace-mcp",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(
                    name="airlock",  # google-client-credentials (Reflector mirrors it here)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def haku_dispatch() -> dict[str, object]:
    name = "haku-dispatch"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Haku dispatch is intentionally parked. Flip this to false only when the
            # worker-zone and provider wiring has been deliberately restored.
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./haku/x/dispatch/deploy",
            prune=True,
            wait=True,
            deletion_policy=KustomizationSpecDeletionPolicy.WAIT_FOR_TERMINATION,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-operator", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="litellm", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="litellm-keys-tf", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="workers-litellm", namespace="haku-dispatch"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="dispatcher", namespace="haku-dispatch"
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def inventree() -> dict[str, object]:
    name = "inventree"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            suspend=True,
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="inventree-app", namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/inventree/app",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="inventree", namespace="inventree"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="inventree-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="inventree-db"),
                KustomizationSpecDependsOn(
                    name="sso-providers-tf",  # writes inventree-sso-providers into the authentik namespace
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="reflector"  # mirrors inventree-sso-providers into the inventree namespace
                ),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def inventree_db() -> dict[str, object]:
    name = "inventree-db"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            suspend=True,
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/inventree/db",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="inventree-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def inventree_namespace() -> dict[str, object]:
    name = "inventree-namespace"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="1h",
            path="./cluster/k8s/parked/inventree/namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="1m",
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def inventree_token_provisioner() -> dict[str, object]:
    name = "inventree-token-provisioner"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            suspend=True,
            interval="10m",
            path="./cluster/k8s/parked/inventree/token-provisioner",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            # Explicitly health-check the InvenTree HelmRelease so the Job starts only after
            # InvenTree pods are ready (HelmRelease wait:true guarantees this).
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="inventree", namespace="inventree"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="inventree"  # InvenTree HelmRelease is fully deployed
                ),
                KustomizationSpecDependsOn(
                    name="claude-rbac",
                    namespace="ducktape-flux",  # claude-sandbox namespace exists
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def kubectl_machine_mcp() -> dict[str, object]:
    name = "kubectl-machine-mcp"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/kubectl-machine-mcp",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="kubectl-machine-mcp",
                    namespace="kubectl-machine-mcp",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                # The kubectl-sandbox-client-credentials Authentik provider (whose OIDC
                # discovery + JWKS this server validates against) is created by this TF.
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def haku_managed_agent() -> dict[str, object]:
    name = "haku-managed-agent"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/parked/managed-agent",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="haku-managed-agent", namespace="haku-sandbox"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="agent-shared-secrets",  # provides ankiweb-credentials in claude-sandbox
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="external-secrets-config",  # provides the claude-sandbox SecretStore
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="haku-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-rbac", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="haku-state",  # provides the haku-forgejo-git secret in haku-sandbox
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="haku-egress-proxy",  # injects the egress proxy + CA the worker imports
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def manifold_mcp() -> dict[str, object]:
    name = "manifold-mcp"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/manifold-mcp",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="manifold-mcp", namespace="manifold-mcp"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def openhands() -> dict[str, object]:
    name = "openhands"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="openhands-app", namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/openhands/app",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="openhands-namespace"),
                KustomizationSpecDependsOn(name="openhands-sandboxes"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-operator", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def openhands_namespace() -> dict[str, object]:
    name = "openhands-namespace"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            path="./cluster/k8s/parked/openhands/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def openhands_sandboxes() -> dict[str, object]:
    name = "openhands-sandboxes"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            path="./cluster/k8s/parked/openhands/sandboxes",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def osm_mcp() -> dict[str, object]:
    name = "osm-mcp"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/osm-mcp",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="osm-mcp"),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="osm-mcp", namespace="osm-mcp"
                ),
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                # forgejo-images-creds-eso.yaml extracts the source Secret from the
                # forgejo-images namespace, so it must exist first.
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def paperless() -> dict[str, object]:
    name = "paperless"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/parked/paperless/app",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="paperless-app", namespace="ducktape-flux"
            ),
            timeout="10m",
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="paperless-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="paperless-cache", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="paperless-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def paperless_cache() -> dict[str, object]:
    name = "paperless-cache"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/paperless/cache",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="paperless-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def paperless_db() -> dict[str, object]:
    name = "paperless-db"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/paperless/db",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="paperless-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def paperless_namespace() -> dict[str, object]:
    name = "paperless-namespace"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            interval="10m",
            path="./cluster/k8s/parked/paperless/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def postscanmail_mcp() -> dict[str, object]:
    name = "postscanmail-mcp"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/postscanmail-mcp",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="postscanmail-mcp", namespace="postscanmail-mcp"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def sdr() -> dict[str, object]:
    name = "sdr"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            # Temporarily disabled until the radio is set up again after relocation.
            suspend=True,
            retry_interval="1m",
            path="./cluster/k8s/parked/sdr",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def tandoor() -> dict[str, object]:
    name = "tandoor"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Suspended — currently using Grocy instead.
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="tandoor-app", namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/tandoor/app",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="tandoor-namespace"),
                KustomizationSpecDependsOn(name="tandoor-db"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def tandoor_db() -> dict[str, object]:
    name = "tandoor-db"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Suspended — currently using Grocy instead.
            suspend=True,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/parked/tandoor/db",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="tandoor-namespace"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def tandoor_namespace() -> dict[str, object]:
    name = "tandoor-namespace"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            # Suspended — currently using Grocy instead.
            suspend=True,
            path="./cluster/k8s/parked/tandoor/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "ducktape-flux", "annotations": {"ducktape.org/parked": "true"}}
    return manifest


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/parked/agent-box/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agent_box())
    path = root / "cluster/k8s/parked/archivebox/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, archivebox())
    path = root / "cluster/k8s/parked/augur-evidence/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, augur_evidence())
    path = root / "cluster/k8s/parked/authelia/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, authelia())
    path = root / "cluster/k8s/parked/browsertrix/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, browsertrix())
    path = root / "cluster/k8s/parked/browsertrix/bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_browsertrix_bucket())
    path = root / "cluster/k8s/parked/browsertrix/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, browsertrix_namespace())
    path = root / "cluster/k8s/parked/browsertrix/retained/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, browsertrix_retained())
    path = root / "cluster/k8s/parked/budget/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, budget())
    path = root / "cluster/k8s/parked/buildbuddy-executor/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, buildbuddy_executor())
    path = root / "cluster/k8s/parked/cloud-agent-tf/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_cloud_agent())
    path = root / "cluster/k8s/parked/docker-ci/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, docker_ci())
    path = root / "cluster/k8s/parked/egress-proxy-rugged/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, egress_proxy_rugged())
    path = root / "cluster/k8s/parked/firecrawl/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, firecrawl())
    path = root / "cluster/k8s/parked/firecrawl/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, firecrawl_db())
    path = root / "cluster/k8s/parked/firecrawl/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, firecrawl_namespace())
    path = root / "cluster/k8s/parked/gecko/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gecko())
    path = root / "cluster/k8s/parked/gecko/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gecko_namespace())
    path = root / "cluster/k8s/parked/google-workspace-mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, google_workspace_mcp())
    path = root / "cluster/k8s/parked/haku-dispatch/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_dispatch())
    path = root / "cluster/k8s/parked/inventree/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, inventree())
    path = root / "cluster/k8s/parked/inventree/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, inventree_db())
    path = root / "cluster/k8s/parked/inventree/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, inventree_namespace())
    path = root / "cluster/k8s/parked/inventree/token-provisioner/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, inventree_token_provisioner())
    path = root / "cluster/k8s/parked/kubectl-machine-mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, kubectl_machine_mcp())
    path = root / "cluster/k8s/parked/managed-agent/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_managed_agent())
    path = root / "cluster/k8s/parked/manifold-mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, manifold_mcp())
    path = root / "cluster/k8s/parked/openhands/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, openhands())
    path = root / "cluster/k8s/parked/openhands/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, openhands_namespace())
    path = root / "cluster/k8s/parked/openhands/sandboxes/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, openhands_sandboxes())
    path = root / "cluster/k8s/parked/osm-mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, osm_mcp())
    path = root / "cluster/k8s/parked/paperless/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, paperless())
    path = root / "cluster/k8s/parked/paperless/cache/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, paperless_cache())
    path = root / "cluster/k8s/parked/paperless/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, paperless_db())
    path = root / "cluster/k8s/parked/paperless/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, paperless_namespace())
    path = root / "cluster/k8s/parked/postscanmail-mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, postscanmail_mcp())
    path = root / "cluster/k8s/parked/sdr/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, sdr())
    path = root / "cluster/k8s/parked/tandoor/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, tandoor())
    path = root / "cluster/k8s/parked/tandoor/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, tandoor_db())
    path = root / "cluster/k8s/parked/tandoor/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, tandoor_namespace())
