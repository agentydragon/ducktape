"""Flux Kustomizations for the cluster/k8s/forgejo slice."""

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


def forgejo_agentydragon_repos(
    chart: Chart, forgejo: Kustomization, tofu_controller: Kustomization, tofu_state_db: Kustomization
) -> Kustomization:
    name = "forgejo-agentydragon-repos"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/forgejo/agentydragon-repos",
            prune=True,
            wait=True,
            # Wait for the Terraform apply (adopts ducktape/gaffer-private, creates
            # collaborator service users/keys, and grants repo access).
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="forgejo-agentydragon-repos",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
            ),
        ),
    )


def forgejo_agentydragon(chart: Chart, tofu_controller: Kustomization, tofu_state_db: Kustomization) -> Kustomization:
    name = "forgejo-agentydragon"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/forgejo/agentydragon",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="forgejo-agentydragon",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db),
        ),
    )


def forgejo(
    chart: Chart,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "forgejo"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/forgejo",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="forgejo", namespace="forgejo"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="forgejo", namespace="forgejo"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="forgejo", namespace="forgejo"
                ),
            ],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            # Admission needs these CRDs; runtime dependencies converge independently.
            depends_on=flux_kustomization_depends_on_many(
                cnpg, external_secrets_operator, seaweedfs_operator, monitoring_crds
            ),
        ),
    )


def budget_ledger(
    chart: Chart,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    budget_namespace: Kustomization,
) -> Kustomization:
    name = "budget-ledger"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/forgejo/budget-ledger",
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the Forgejo repo + service user + the
            # budget-ledger-git-creds Secret) so the exporter/Fava can depend on it.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="budget-ledger",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # the git-creds Secret lands in the budget namespace
                budget_namespace,
            ),
        ),
    )


def budget_namespace(chart: Chart) -> Kustomization:
    name = "budget-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/forgejo/budget-namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="1m",
        ),
    )


def forgejo_cache(chart: Chart, valkey: Kustomization, local_path_provisioner: Kustomization) -> Kustomization:
    name = "forgejo-cache"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/forgejo/cache",
            prune=True,
            wait=True,
            # Retry until the forgejo aggregate creates the Namespace. Waiting for
            # Forgejo readiness would deadlock its cache-dependent startup.
            depends_on=flux_kustomization_depends_on_many(valkey, local_path_provisioner),
        ),
    )


def forgejo_claude(
    chart: Chart,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    claude_rbac: Kustomization,
) -> Kustomization:
    name = "forgejo-claude"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/forgejo/claude",
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the claude Forgejo service user + the
            # claude-forgejo-credentials Secret) so repo read-grants (e.g. gaffer-private
            # tf/thrive-scrape) and agent sessions can depend on it.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="forgejo-claude",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # the credentials Secret lands in claude-sandbox
                claude_rbac,
            ),
        ),
    )


def cpap_data(
    chart: Chart,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    cpap_sync: Kustomization,
) -> Kustomization:
    name = "cpap-data"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/forgejo/cpap-data",
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the Forgejo repo + service users + the
            # cpap-data-git-{write,read} Secrets) so the sync CronJob can depend on it.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="cpap-data",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # the git-creds Secrets land in the cpap-sync namespace
                cpap_sync,
            ),
        ),
    )


def haku_state(
    chart: Chart,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    agentplane_index: Kustomization,
    haku_namespace: Kustomization,
) -> Kustomization:
    name = "haku-state"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/forgejo/haku-state",
            prune=True,
            wait=True,
            # Wait for the Terraform apply (creates the Forgejo repo + service user + the
            # haku-forgejo-git Secret) so scan runs can depend on it.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="haku-state",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
                # The git-creds Secret is reflected into agentplane-index; wait for the
                # aggregate to create that target Namespace before applying Terraform.
                agentplane_index,
                haku_namespace,
            ),
        ),
    )
