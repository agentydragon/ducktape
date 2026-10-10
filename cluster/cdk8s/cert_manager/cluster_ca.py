"""The cluster's self-signed root CA: the bootstrap ClusterIssuer that signs it, its
Certificate, the `cluster-internal-ca` ClusterIssuer that issues from it, and the
trust-manager Bundle that distributes it, with the active Let's Encrypt root, to every
namespace.
"""

from __future__ import annotations

from typing import TypedDict

from cdk8s import ApiObjectMetadata, App, Chart
from cert_manager_clusterissuer_crds.io.cert_manager import ClusterIssuerSpecCa, ClusterIssuerSpecSelfSigned
from cert_manager_crds.io.cert_manager import (
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
)
from trust_manager_crds.io.cert_manager.trust import (
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
)

from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cert_manager.certificate import Certificate
from cluster.cdk8s.providers.cert_manager.cluster_issuer import ClusterIssuer

NAME = "cluster-ca"
ROOT_CA_SECRET = "cluster-root-ca-secret"
# The issuer for leaf certificates, and the ConfigMap (in every namespace) holding the roots
# that verify them.
INTERNAL_ISSUER = CertificateSpecIssuerRef(name="cluster-internal-ca", kind="ClusterIssuer")
BUNDLE = "cluster-internal-ca-bundle"
BUNDLE_KEY = "ca-certificates.crt"


class _LongLivedCa(TypedDict):
    duration: str
    renew_before: str


# Every long-lived CA Certificate this cluster signs, spread as `Certificate(..., **LONG_LIVED_CA)`.
LONG_LIVED_CA: _LongLivedCa = {"duration": "87600h", "renew_before": "8760h"}  # 10 years / 1 year


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    bootstrap = ClusterIssuer(
        chart,
        "bootstrap",
        metadata=ApiObjectMetadata(name="cluster-ca-bootstrap"),
        self_signed=ClusterIssuerSpecSelfSigned(),
    )
    Certificate(
        chart,
        "root-ca",
        metadata=ApiObjectMetadata(name="cluster-root-ca", namespace="cert-manager"),
        is_ca=True,
        common_name="cluster-root-ca",
        secret_name=ROOT_CA_SECRET,
        **LONG_LIVED_CA,
        private_key=CertificateSpecPrivateKey(algorithm=CertificateSpecPrivateKeyAlgorithm.RSA, size=4096),
        issuer_ref=CertificateSpecIssuerRef(name=bootstrap.name, kind="ClusterIssuer"),
    )
    # Issues internal service certificates from the root CA.
    ClusterIssuer(
        chart,
        "internal",
        metadata=ApiObjectMetadata(name=INTERNAL_ISSUER.name),
        ca=ClusterIssuerSpecCa(secret_name=ROOT_CA_SECRET),
    )
    Bundle(
        chart,
        "bundle",
        metadata=ApiObjectMetadata(name=BUNDLE),
        sources=[
            BundleSpecSources(secret=BundleSpecSourcesSecret(name=ROOT_CA_SECRET, key="ca.crt")),
            # The active issuer's root, from `config`.
            BundleSpecSources(secret=BundleSpecSourcesSecret(name=f"{LETSENCRYPT_ISSUER}-root-ca", key="ca.crt")),
        ],
        target=BundleSpecTarget(config_map=BundleSpecTargetConfigMap(key=BUNDLE_KEY)),
    )
    return chart
