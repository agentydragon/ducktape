"""The shared Kubernetes-provider ClusterSecretStores, each reading one source namespace.

Every store here declares `conditions`. Without it a store is usable from ANY namespace,
so an ExternalSecret added anywhere can pull any Secret out of the store's remoteNamespace
— and the ESO ServiceAccount holds cluster-wide secret read, so the store, not RBAC, is the
only thing bounding that. Keep each list to the namespaces that actually hold a consuming
ExternalSecret — ESO refuses to reconcile into any namespace not listed here, so an
omission shows up as a failing ExternalSecret. That every store declares `conditions` at
all is enforced at admission by the `require-secret-store-conditions` Kyverno policy.

Application-scoped stores live with their applications (`ntfy.py`, ...).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from external_secret_store_crds.io.external_secrets import (
    ClusterSecretStore,
    ClusterSecretStoreSpec,
    ClusterSecretStoreSpecConditions,
    ClusterSecretStoreSpecProvider,
    ClusterSecretStoreSpecProviderKubernetes,
    ClusterSecretStoreSpecProviderKubernetesAuth,
    ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount,
    ClusterSecretStoreSpecProviderKubernetesServer,
    ClusterSecretStoreSpecProviderKubernetesServerCaProvider,
    ClusterSecretStoreSpecProviderKubernetesServerCaProviderType,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts

NAME = "external-secrets-config"
OUTPUT_DIR = "cluster/k8s/external-secrets/config"
_ESO_SERVICE_ACCOUNT = ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(
    name="external-secrets", namespace="external-secrets-system"
)


def _store(
    chart: Chart,
    name: str,
    *,
    namespaces: Sequence[str],
    remote_namespace: str,
    service_account: ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount = _ESO_SERVICE_ACCOUNT,
) -> None:
    ClusterSecretStore(
        chart,
        name,
        metadata=ApiObjectMetadata(name=name),
        spec=ClusterSecretStoreSpec(
            conditions=[ClusterSecretStoreSpecConditions(namespaces=list(namespaces))],
            provider=ClusterSecretStoreSpecProvider(
                kubernetes=ClusterSecretStoreSpecProviderKubernetes(
                    server=ClusterSecretStoreSpecProviderKubernetesServer(
                        ca_provider=ClusterSecretStoreSpecProviderKubernetesServerCaProvider(
                            type=ClusterSecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            name="kube-root-ca.crt",
                            key="ca.crt",
                            namespace="default",
                        )
                    ),
                    auth=ClusterSecretStoreSpecProviderKubernetesAuth(service_account=service_account),
                    remote_namespace=remote_namespace,
                )
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _store(
        chart,
        "kubernetes-cli-proxy-api-secret-store",
        namespaces=["cli-proxy-api", "litellm"],
        remote_namespace="cli-proxy-api",
    )
    # Mirrors the read-only ActivityWatch bearer into Haku's own namespace
    # (haku/workspaces.py).
    #
    # ESO rather than the reflector annotations on the source Secret: the source is
    # SOPS-encrypted to the cluster key only, and an agent without that key cannot
    # re-MAC an annotation edit — a scoped store is the distribution path that agents
    # can author (cluster/AGENTS.md § Annotating a SOPS-encrypted Secret).
    #
    # activitywatch holds two Secrets: the read token (mirrored here) and the write
    # token (never mirrored anywhere — a namespace on this list can pull either, so
    # keep it to Haku's own namespace and let the consumer's ExternalSecret name
    # only the read token).
    _store(
        chart, "kubernetes-activitywatch-secret-store", namespaces=["haku-sandbox"], remote_namespace="activitywatch"
    )
    # Airlock brokers short-lived OAuth access tokens; the agentplane-staging egress proxy
    # (via its isolated credentials namespace) and google-mcp (the write-scoped Google
    # Gmail/Calendar MCP backend, cluster/cdk8s/google_mcp.py) consume them.
    _store(
        chart,
        "kubernetes-airlock-secret-store",
        namespaces=["agentplane-staging-egress-credentials", "google-mcp"],
        remote_namespace="airlock",
    )
    _store(
        chart,
        "kubernetes-external-creds-secret-store",
        # Source-side RoleBindings remain authoritative. Keep this defense-in-depth
        # list equal to the namespaces approved by external-creds grants.
        namespaces=[
            "agentplane-staging-egress-credentials",
            "agentplane-staging",
            "agentplane-testing-egress-credentials",
            "agents-infra",
            "cert-manager",
            "claude-sandbox",
            "flux-system",
            "haku-console",
            "haku-egress-proxy",
            "haku-sandbox",
            "litellm",
            "monitoring",
            "nix-cache",
            "public-coder-agent",
            "tana-mcp",
        ],
        remote_namespace="ducktape-flux",
        # Referent authentication resolves this identity in each consuming
        # ExternalSecret's namespace. Access still requires a source-side grant.
        service_account=ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(name="external-creds-reader"),
    )
    # Mirrors rotator-published tokens (authentik-jwt-rotation k8s_secret outputs) into
    # consumer namespaces without coupling distribution to the rotator.
    #
    # Widest store in the cluster: flux-system holds the GitHub App, four PATs, the AWS
    # Route 53 credentials and ci-age-key, so an absent `conditions` here is a cluster-wide
    # read path to all of them. Keep this list minimal.
    #
    # Before adding a namespace, check what it actually needs. A namespace that only wants
    # the forgejo-images-creds pull secret belongs on the scoped
    # kubernetes-forgejo-images-secret-store instead — that grants one dockerconfigjson,
    # where an entry here grants every Secret in flux-system. haku-egress-proxy,
    # haku-openclaw-spike and osm-mcp were each on this list for exactly that reason and
    # have moved.
    _store(
        chart,
        "kubernetes-flux-system-secret-store",
        namespaces=[
            # alloy-otlp-bearer (ClusterExternalSecret, both)
            "claude-sandbox",
            # also haku-mail-token, and forgejo-images-creds — the last of which
            # stays on ESO only because the one above already put it here.
            "haku-sandbox",
        ],
        remote_namespace="flux-system",
    )
    # Every namespace that pulls a private git.allegedly.works/ducktape-ci/* image reads
    # the source Secret through here via its own ExternalSecret (named
    # forgejo-images-creds-eso.yaml, next to that namespace's other manifests) — this
    # replaced the old emberstack-reflector fan-out on
    # cluster/k8s/forgejo-images/registry-creds.sops.yaml, which required SOPS-decrypting a
    # shared ciphertext file to add a namespace. Keep this list to genuine consumers.
    _store(
        chart,
        "kubernetes-forgejo-images-secret-store",
        namespaces=[
            "activitywatch",
            "agent-workspaces",
            "agentplane-index",
            "agentplane-staging",
            "agentplane-testing",
            "agents-infra",
            "airlock",
            "cli-proxy-api",
            "cpap-sync",
            "flux-system",
            "github-api-proxy",
            "google-mcp",
            "grocy-sf",
            "grocy-vallejo",
            "ha-mcp",
            "haku-console",
            "haku-egress-proxy",
            "haku-mailbox",
            "haku-openclaw-spike",
            "home-assistant",
            "litellm",
            "loki-read-proxy",
            "matrix",
            "monitoring",
            "nix-cache",
            "plaid-mcp",
            "props",
            "public-coder-agent",
            "sdr",
            "ssh-mcp",
            "study-casino",
            "tana-mcp",
            "wayback-cache",
        ],
        remote_namespace="forgejo-images",
    )
    # google-mcp mints its own caller-facing bearer (cluster/cdk8s/google_mcp.py); only
    # agentplane-staging's Action Service (the only caller) reads a copy.
    _store(
        chart, "kubernetes-google-mcp-secret-store", namespaces=["agentplane-staging"], remote_namespace="google-mcp"
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def external_secrets_config(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, external_secrets_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        interval="10m0s",
        retry_interval="30s",
        timeout="5m0s",
        # Health-check a representative shared ClusterSecretStore before dependents run.
        # Application-scoped stores are owned and checked by their app Kustomizations.
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ClusterSecretStore",
                name="kubernetes-flux-system-secret-store",
            )
        ],
        depends_on=[flux_kustomization_depends_on(external_secrets_operator)],
    )
