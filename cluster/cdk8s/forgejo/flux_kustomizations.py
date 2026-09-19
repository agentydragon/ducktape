"""Flux Kustomizations for the cluster/k8s/forgejo slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def forgejo_agentydragon_repos() -> dict[str, object]:
    name = "forgejo-agentydragon-repos"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo API must be up (provider target)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
            ],
        ),
    )


def forgejo_agentydragon() -> dict[str, object]:
    name = "forgejo-agentydragon"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
            ],
        ),
    )


def forgejo() -> dict[str, object]:
    name = "forgejo"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/forgejo/app",
            prune=True,
            wait=True,
            # Health check ensures Forgejo is fully operational with SSO
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
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="forgejo-cache",  # shared valkey for cache + queue (HA prerequisite)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="seaweedfs-cluster",  # SeaweedFS CRDs, operator, and backend
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="reflector",  # Mirrors forgejo-oauth-client-secret into the namespace
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="sso-providers-tf",  # Writes forgejo-oauth-client-secret to authentik namespace
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="authentik",  # Authentik must be running for OIDC
                    namespace="ducktape-flux",
                ),
                # Forgejo validates OIDC at init time (the configure init container fetches the discovery URL).
                # Other SSO apps (Matrix, Grafana) validate lazily at first login, so they don't need this.
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor/PodMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def budget_ledger() -> dict[str, object]:
    name = "budget-ledger"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo API must be up (provider target)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="budget-namespace",  # the git-creds Secret lands in the budget namespace
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def budget_namespace() -> dict[str, object]:
    name = "budget-namespace"
    return flux_kustomization(
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


def forgejo_cache() -> dict[str, object]:
    name = "forgejo-cache"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def forgejo_claude() -> dict[str, object]:
    name = "forgejo-claude"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo API must be up (provider target)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="claude-rbac",  # the credentials Secret lands in claude-sandbox
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def cpap_data() -> dict[str, object]:
    name = "cpap-data"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo API must be up (provider target)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="cpap-sync",  # the git-creds Secrets land in the cpap-sync namespace
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def forgejo_db() -> dict[str, object]:
    name = "forgejo-db"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/forgejo/db",
            prune=True,
            wait=True,
            # Required to apply forgejo-db-ssd-creds.sops.yaml (Forgejo's DB app creds, which CNPG
            # syncs onto the -ssd forgejo role); without it Flux applies the ciphertext.
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
            ],
        ),
    )


def haku_state() -> dict[str, object]:
    name = "haku-state"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo API must be up (provider target)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                # The git-creds Secret is reflected into agentplane-index; wait for the
                # aggregate to create that target Namespace before applying Terraform.
                KustomizationSpecDependsOn(name="agentplane-index", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-console-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-egress-proxy-namespace", namespace="ducktape-flux"),
            ],
        ),
    )


def forgejo_namespace() -> dict[str, object]:
    name = "forgejo-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/forgejo/namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="1m",
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/forgejo/agentydragon-repos/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_agentydragon_repos())
    path = root / "cluster/k8s/forgejo/agentydragon/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_agentydragon())
    path = root / "cluster/k8s/forgejo/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo())
    path = root / "cluster/k8s/forgejo/budget-ledger/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, budget_ledger())
    path = root / "cluster/k8s/forgejo/budget-namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, budget_namespace())
    path = root / "cluster/k8s/forgejo/cache/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_cache())
    path = root / "cluster/k8s/forgejo/claude/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_claude())
    path = root / "cluster/k8s/forgejo/cpap-data/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cpap_data())
    path = root / "cluster/k8s/forgejo/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_db())
    path = root / "cluster/k8s/forgejo/haku-state/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_state())
    path = root / "cluster/k8s/forgejo/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_namespace())
