"""The shared shape of every TLS-interception root CA in this cluster (mitmproxy,
haku-egress-proxy, public-coder-agent's iron-proxy, the Agentplane egress proxy): a
`cluster-ca-bootstrap`-issued, 10-year ECDSA P-256 self-signed CA, reflected by
trust-manager into `reflection_namespaces` and published as a `ca-certificates.crt`
trust bundle ConfigMap into `target_namespaces`. Callers differ only in naming, which
namespaces read the reflected Secret and the published bundle, and (Agentplane) an extra
PKCS12 target format for its JVM truststore consumer.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cert_manager_crds.io.cert_manager import CertificateSpecIssuerRef, CertificateSpecSecretTemplate
from constructs import Construct
from trust_manager_crds.io.cert_manager.trust import (
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetAdditionalFormats,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from cluster.cdk8s.cert_manager.cluster_ca import LONG_LIVED_CA, ROOT_CA_SECRET
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.reflector import mirror_annotations

_ROOT_CA_ISSUER = "cluster-ca-bootstrap"
BUNDLE_KEY = "ca-certificates.crt"


def interception_root_ca(
    scope: Construct,
    *,
    name: str,
    namespace: str,
    secret_name: str,
    bundle_name: str,
    description: str,
    target_namespaces: Sequence[str],
    reflection_namespaces: Sequence[str] = ("cert-manager",),
    additional_formats: BundleSpecTargetAdditionalFormats | None = None,
) -> None:
    """Our policy: ECDSA P-256, 10-year duration, 1-year renewal window.

    `reflection_namespaces` is where trust-manager may read the reflected CA Secret from
    (always at least `cert-manager`, its own namespace); `target_namespaces` is where the
    published trust bundle ConfigMap lands.
    """
    Certificate(
        scope,
        "certificate",
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        is_ca=True,
        common_name=name,
        secret_name=secret_name,
        **LONG_LIVED_CA,
        private_key=CertificatePrivateKey.ecdsa_p256(),
        # trust-manager reads Bundle sources from its own namespace.
        secret_template=CertificateSpecSecretTemplate(annotations=mirror_annotations(reflection_namespaces)),
        issuer_ref=CertificateSpecIssuerRef(name=_ROOT_CA_ISSUER, kind="ClusterIssuer"),
    )
    Bundle(
        scope,
        "trust-bundle",
        metadata=ApiObjectMetadata(name=bundle_name),
        sources=[
            BundleSpecSources(use_default_c_as=True),
            BundleSpecSources(secret=BundleSpecSourcesSecret(name=ROOT_CA_SECRET, key="ca.crt")),
            BundleSpecSources(secret=BundleSpecSourcesSecret(name=secret_name, key="tls.crt")),
        ],
        target=BundleSpecTarget(
            config_map=BundleSpecTargetConfigMap(
                key=BUNDLE_KEY, metadata=BundleSpecTargetConfigMapMetadata(annotations={"description": description})
            ),
            additional_formats=additional_formats,
            namespace_selector=BundleSpecTargetNamespaceSelector(
                match_expressions=[
                    BundleSpecTargetNamespaceSelectorMatchExpressions(
                        key="kubernetes.io/metadata.name", operator="In", values=list(target_namespaces)
                    )
                ]
            ),
        ),
    )
