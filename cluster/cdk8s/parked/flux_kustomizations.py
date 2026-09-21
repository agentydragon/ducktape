"""Flux Kustomizations for the cluster/k8s/parked slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def agent_box(
    chart: Chart,
    kubevirt: Kustomization,
    cdi: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_public_s3: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
    name = "agent-box"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                kubevirt, cdi, external_secrets_operator, seaweedfs_public_s3, local_path_provisioner
            ),
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


def archivebox(chart: Chart, seaweedfs_csi: Kustomization, local_path_provisioner: Kustomization) -> Kustomization:
    name = "archivebox"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(seaweedfs_csi, local_path_provisioner),
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


def augur_evidence(
    chart: Chart,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    budget_namespace: Kustomization,
) -> Kustomization:
    name = "augur-evidence"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # the git-creds Secrets land in the budget namespace
                budget_namespace,
            ),
        ),
    )


def browsertrix(
    chart: Chart,
    browsertrix_namespace: Kustomization,
    seaweedfs_browsertrix_bucket: Kustomization,
    seaweedfs_secrets: Kustomization,
    reflector: Kustomization,
    local_path_provisioner: Kustomization,
    seaweedfs_csi: Kustomization,
) -> Kustomization:
    name = "browsertrix"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                browsertrix_namespace,
                seaweedfs_browsertrix_bucket,
                seaweedfs_secrets,
                reflector,
                local_path_provisioner,
                seaweedfs_csi,
            ),
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


def seaweedfs_browsertrix_bucket(
    chart: Chart, seaweedfs_cluster: Kustomization, seaweedfs_secrets: Kustomization
) -> Kustomization:
    # Retain the existing resource name so moving its source path does not delete
    # the live Bucket before its explicit retirement.
    name = "seaweedfs-browsertrix-bucket"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(seaweedfs_cluster, seaweedfs_secrets),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="browsertrix", namespace="seaweedfs"
                )
            ],
        ),
    )


def browsertrix_namespace(chart: Chart) -> Kustomization:
    name = "browsertrix-namespace"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def browsertrix_retained(chart: Chart) -> Kustomization:
    name = "browsertrix-retained"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def budget(
    chart: Chart, budget_ledger: Kustomization, gateway: Kustomization, authentik: Kustomization
) -> Kustomization:
    name = "budget"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                # provisions budget-ledger-git-creds in the budget ns
                budget_ledger,
                gateway,
                authentik,
            ),
        ),
    )


def buildbuddy_executor(chart: Chart) -> Kustomization:
    name = "buildbuddy-executor"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def haku_cloud_agent(
    chart: Chart,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
) -> Kustomization:
    name = "haku-cloud-agent"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                external_creds, external_secrets_config, tofu_controller, tofu_state_db
            ),
        ),
    )


def docker_ci(chart: Chart, cert_manager_environment: Kustomization, claude_rbac: Kustomization) -> Kustomization:
    name = "docker-ci"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                # No storage dep (emptyDir, not a CSI PVC). Needs the cluster-internal-ca
                # ClusterIssuer for the mTLS Certificates and agent-rbac-base for the
                # claude-sandbox namespace the client Certificate lives in.
                cert_manager_environment,
                claude_rbac,
            ),
        ),
    )


def gecko(
    chart: Chart,
    gecko_namespace: Kustomization,
    kubevirt: Kustomization,
    cdi: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_public_s3: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
    name = "gecko"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                gecko_namespace, kubevirt, cdi, external_secrets_operator, seaweedfs_public_s3, local_path_provisioner
            ),
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


def gecko_namespace(chart: Chart) -> Kustomization:
    name = "gecko-namespace"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def google_workspace_mcp(
    chart: Chart, airlock: Kustomization, local_path_provisioner: Kustomization, reflector: Kustomization
) -> Kustomization:
    name = "google-workspace-mcp"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                # google-client-credentials (Reflector mirrors it here)
                airlock,
                local_path_provisioner,
                reflector,
            ),
        ),
    )


def haku_dispatch(
    chart: Chart,
    cnpg: Kustomization,
    local_path_provisioner: Kustomization,
    external_secrets_config: Kustomization,
    external_secrets_operator: Kustomization,
    litellm: Kustomization,
    litellm_keys_tf: Kustomization,
) -> Kustomization:
    name = "haku-dispatch"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                cnpg,
                local_path_provisioner,
                external_secrets_config,
                external_secrets_operator,
                litellm,
                litellm_keys_tf,
            ),
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


def inventree(
    chart: Chart,
    forgejo_images: Kustomization,
    inventree_namespace: Kustomization,
    inventree_db: Kustomization,
    sso_providers_tf: Kustomization,
    reflector: Kustomization,
    gateway: Kustomization,
    authentik: Kustomization,
) -> Kustomization:
    name = "inventree"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                forgejo_images,
                inventree_namespace,
                inventree_db,
                # writes inventree-sso-providers into the authentik namespace
                sso_providers_tf,
                # mirrors inventree-sso-providers into the inventree namespace
                reflector,
                gateway,
                authentik,
            ),
        ),
    )


def inventree_db(
    chart: Chart, inventree_namespace: Kustomization, cnpg: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "inventree-db"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(inventree_namespace, cnpg, local_path_provisioner),
        ),
    )


def inventree_namespace(chart: Chart) -> Kustomization:
    name = "inventree-namespace"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def inventree_token_provisioner(
    chart: Chart,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    inventree: Kustomization,
    claude_rbac: Kustomization,
) -> Kustomization:
    name = "inventree-token-provisioner"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                # InvenTree HelmRelease is fully deployed
                inventree,
                # claude-sandbox namespace exists
                claude_rbac,
            ),
        ),
    )


def haku_managed_agent(
    chart: Chart,
    forgejo_images: Kustomization,
    agent_shared_secrets: Kustomization,
    external_secrets_config: Kustomization,
    haku_namespace: Kustomization,
    haku_rbac: Kustomization,
    haku_state: Kustomization,
    haku_egress_proxy: Kustomization,
) -> Kustomization:
    name = "haku-managed-agent"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                forgejo_images,
                # provides ankiweb-credentials in claude-sandbox
                agent_shared_secrets,
                # provides the claude-sandbox SecretStore
                external_secrets_config,
                haku_namespace,
                haku_rbac,
                # provides the haku-forgejo-git secret in haku-sandbox
                haku_state,
                # injects the egress proxy + CA the worker imports
                haku_egress_proxy,
            ),
        ),
    )


def openhands(
    chart: Chart,
    openhands_namespace: Kustomization,
    openhands_sandboxes: Kustomization,
    gateway: Kustomization,
    authentik: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "openhands"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(
                openhands_namespace, openhands_sandboxes, gateway, authentik, external_secrets_operator
            ),
        ),
    )


def openhands_namespace(chart: Chart) -> Kustomization:
    name = "openhands-namespace"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def openhands_sandboxes(chart: Chart) -> Kustomization:
    name = "openhands-sandboxes"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def paperless(
    chart: Chart,
    paperless_namespace: Kustomization,
    paperless_cache: Kustomization,
    paperless_db: Kustomization,
    gateway: Kustomization,
) -> Kustomization:
    name = "paperless"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(paperless_namespace, paperless_cache, paperless_db, gateway),
        ),
    )


def paperless_cache(
    chart: Chart, paperless_namespace: Kustomization, valkey: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "paperless-cache"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(paperless_namespace, valkey, local_path_provisioner),
        ),
    )


def paperless_db(
    chart: Chart, paperless_namespace: Kustomization, cnpg: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "paperless-db"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(paperless_namespace, cnpg, local_path_provisioner),
        ),
    )


def paperless_namespace(chart: Chart) -> Kustomization:
    name = "paperless-namespace"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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


def sdr(
    chart: Chart,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    authentik: Kustomization,
) -> Kustomization:
    name = "sdr"
    return flux_kustomization(
        chart,
        name,
        annotations={"ducktape.org/parked": "true"},
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
            depends_on=flux_kustomization_depends_on_many(external_secrets_config, forgejo_images, gateway, authentik),
        ),
    )
