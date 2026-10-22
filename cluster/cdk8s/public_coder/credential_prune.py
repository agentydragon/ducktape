"""Temporary inventory for pruning retired Public Coder credentials without storing their values."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from trust_manager_crds.io.cert_manager.trust import (
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from cluster.cdk8s.cert_manager.cluster_ca import LONG_LIVED_CA, ROOT_CA_SECRET
from cluster.cdk8s.cert_manager.interception_ca import BUNDLE_KEY
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cert_manager.certificate import (
    Certificate,
    CertificatePrivateKey,
    CertificateSpecIssuerRef,
    CertificateSpecSecretTemplate,
)
from cluster.cdk8s.providers.external_secrets.external_secret import (
    ExternalSecret,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    SecretStoreRef,
    remote_data,
)
from cluster.cdk8s.reflector import mirror_annotations

NAME = "public-coder-agent-secret-prune"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/public-coder-agent/secret-prune"
NAMESPACE = "public-coder-agent"
_MERGE_ANNOTATION = {"kustomize.toolkit.fluxcd.io/ssa": "Merge"}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    brave_search_api_key = "brave-search-api-key"
    ExternalSecret(
        chart,
        brave_search_api_key,
        metadata=ApiObjectMetadata(name=brave_search_api_key, namespace=NAMESPACE, annotations=_MERGE_ANNOTATION),
        refresh_interval="1m",
        secret_store_ref=SecretStoreRef.cluster("kubernetes-external-creds-secret-store"),
        data=[remote_data(brave_search_api_key, "api-key")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    github_token = "public-coder-agent-github-token"
    ExternalSecret(
        chart,
        github_token,
        metadata=ApiObjectMetadata(name=github_token, namespace=NAMESPACE, annotations=_MERGE_ANNOTATION),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster("kubernetes-external-creds-secret-store"),
        data=[remote_data("github-agentydragon-agent", "token", secret_key="GITHUB_TOKEN")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )

    certificate_name = "public-coder-agent-proxy-root-ca"
    proxy_ca_secret = "public-coder-agent-proxy-ca"
    Certificate(
        chart,
        "proxy-root-ca",
        metadata=ApiObjectMetadata(name=certificate_name, namespace=NAMESPACE, annotations=_MERGE_ANNOTATION),
        is_ca=True,
        common_name=certificate_name,
        secret_name=proxy_ca_secret,
        **LONG_LIVED_CA,
        private_key=CertificatePrivateKey.ecdsa_p256(),
        secret_template=CertificateSpecSecretTemplate(annotations=mirror_annotations(("cert-manager",))),
        issuer_ref=CertificateSpecIssuerRef(name="cluster-ca-bootstrap", kind="ClusterIssuer"),
    )
    Bundle(
        chart,
        "proxy-ca-bundle",
        metadata=ApiObjectMetadata(name="public-coder-agent-proxy-ca-cert", annotations=_MERGE_ANNOTATION),
        sources=[
            BundleSpecSources(use_default_c_as=True),
            BundleSpecSources(secret=BundleSpecSourcesSecret(name=ROOT_CA_SECRET, key="ca.crt")),
            BundleSpecSources(secret=BundleSpecSourcesSecret(name=proxy_ca_secret, key="tls.crt")),
        ],
        target=BundleSpecTarget(
            config_map=BundleSpecTargetConfigMap(
                key=BUNDLE_KEY,
                metadata=BundleSpecTargetConfigMapMetadata(
                    annotations={
                        "description": "Trust bundle for public-coder-agent HTTPS traffic intercepted by its egress proxy"
                    }
                ),
            ),
            namespace_selector=BundleSpecTargetNamespaceSelector(
                match_expressions=[
                    BundleSpecTargetNamespaceSelectorMatchExpressions(
                        key="kubernetes.io/metadata.name", operator="In", values=[NAMESPACE]
                    )
                ]
            ),
        ),
    )

    for name, secret_type in (
        (brave_search_api_key, "Opaque"),
        (github_token, "Opaque"),
        (proxy_ca_secret, "kubernetes.io/tls"),
    ):
        k8s.KubeSecret(
            chart,
            f"secret-{name}",
            metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE, annotations=_MERGE_ANNOTATION),
            type=secret_type,
        )
    return chart
