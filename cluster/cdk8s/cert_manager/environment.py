"""cert-manager's environment: the Route 53 credentials its ACME DNS-01 solvers read,
copied from external-creds, plus the ClusterIssuers and cluster CA from `config/base`
and `cluster-ca/base`, which the directory's `kustomization.yaml` pulls in."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecHealthChecks,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "cert-manager-environment"
NAMESPACE = "cert-manager"
OUTPUT_DIR = "cluster/k8s/cert-manager/environment"
_ROUTE53_SECRET = "aws-route53-credentials"
_ROUTE53_SOURCE = "aws-route53-cert-manager-credentials"


def chart(app: App) -> Chart:
    chart = Chart(app, "environment", disable_resource_name_hashes=True)
    # Consumer-owned identity for reading approved canonical credentials.
    k8s.KubeServiceAccount(chart, "reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE))
    ExternalSecret(
        chart,
        "route53-credentials",
        metadata=metadata(_ROUTE53_SECRET, NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-external-creds-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(
                name=_ROUTE53_SECRET, creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key=key, remote_ref=ExternalSecretSpecDataRemoteRef(key=_ROUTE53_SOURCE, property=key)
                )
                for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["environment.k8s.yaml", "../config/base", "../cluster-ca/base"]),
    )


def cert_manager_environment(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    cert_manager_issuer_config: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="5m",
        post_build=KustomizationSpecPostBuild(
            substitute_from=[
                KustomizationSpecPostBuildSubstituteFrom(
                    kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                )
            ]
        ),
        depends_on=flux_kustomization_depends_on_many(
            cert_manager, cert_manager_trust, cert_manager_issuer_config, external_creds, external_secrets_config
        ),
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="cert-manager.io/v1", kind="ClusterIssuer", name="letsencrypt-prod", namespace=""
            ),
            KustomizationSpecHealthChecks(
                api_version="cert-manager.io/v1", kind="ClusterIssuer", name="letsencrypt-staging", namespace=""
            ),
        ],
    )
