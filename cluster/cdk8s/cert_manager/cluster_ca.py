"""The cluster's self-signed root CA: the bootstrap ClusterIssuer that signs it, its
Certificate, the `cluster-internal-ca` ClusterIssuer that issues from it, and the
trust-manager Bundle that distributes it, with the active Let's Encrypt root, to every
namespace.

Beside it, the Agentplane workload CA: an intermediate under the cluster root behind the
`agentplane-workload` ClusterIssuer, which every Agentplane SVID comes from. Its leaves chain to the
cluster root, so clients verifying an Agentplane server certificate keep the cluster Bundle; its
own Bundle carries the intermediate alone, for verifiers of client SVIDs, so a leaf the cluster root
signs directly does not authenticate there. Who may request from it is Agentplane admission
(`cluster/cdk8s/agentplane/workload_certificate_admission.py`).
"""

from __future__ import annotations

from typing import TypedDict

from cdk8s import ApiObjectMetadata, App, Chart
from cert_manager_clusterissuer_crds.io.cert_manager import ClusterIssuerSpecCa, ClusterIssuerSpecSelfSigned
from cert_manager_crds.io.cert_manager import (
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from constructs import Construct
from trust_manager_crds.io.cert_manager.trust import (
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
)

from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.providers.cert_manager.cluster_issuer import ClusterIssuer

NAME = "cluster-ca"
ROOT_CA_SECRET = "cluster-root-ca-secret"
# The issuer for leaf certificates, and the ConfigMap (in every namespace) holding the roots
# that verify them.
INTERNAL_ISSUER = CertificateSpecIssuerRef(name="cluster-internal-ca", kind="ClusterIssuer")
BUNDLE = "cluster-internal-ca-bundle"
BUNDLE_KEY = "ca-certificates.crt"

WORKLOAD_ISSUER = CertificateSpecIssuerRef(name="agentplane-workload", kind="ClusterIssuer")
WORKLOAD_BUNDLE = "agentplane-workload-ca-bundle"
_WORKLOAD_CA_SECRET = "agentplane-workload-ca"


class _LongLivedCa(TypedDict):
    duration: str
    renew_before: str


# Every long-lived CA Certificate this cluster signs, spread as `Certificate(..., **LONG_LIVED_CA)`.
LONG_LIVED_CA: _LongLivedCa = {"duration": "87600h", "renew_before": "8760h"}  # 10 years / 1 year


def add_workload_ca(scope: Construct) -> None:
    """The Agentplane workload CA, its ClusterIssuer, and the Bundle carrying it alone to every namespace."""
    Certificate(
        scope,
        "workload-ca",
        # A ClusterIssuer reads its CA from cert-manager's cluster resource namespace.
        metadata=ApiObjectMetadata(name=_WORKLOAD_CA_SECRET, namespace="cert-manager"),
        is_ca=True,
        common_name=_WORKLOAD_CA_SECRET,
        secret_name=_WORKLOAD_CA_SECRET,
        **LONG_LIVED_CA,
        # A renewal keeps the key, so leaves issued before it still verify against the Bundles.
        private_key=CertificatePrivateKey.ecdsa_p256(CertificateSpecPrivateKeyRotationPolicy.NEVER),
        usages=[CertificateSpecUsages.CERT_SIGN, CertificateSpecUsages.CRL_SIGN],
        issuer_ref=INTERNAL_ISSUER,
    )
    ClusterIssuer(
        scope,
        "workload-issuer",
        metadata=ApiObjectMetadata(name=WORKLOAD_ISSUER.name),
        ca=ClusterIssuerSpecCa(secret_name=_WORKLOAD_CA_SECRET),
    )
    Bundle(
        scope,
        "workload-bundle",
        metadata=ApiObjectMetadata(name=WORKLOAD_BUNDLE),
        # `tls.crt` of a CA the cluster root signs holds that CA alone: cert-manager leaves the
        # self-signed root out of the chain, and `ca.crt` is the root.
        sources=[BundleSpecSources(secret=BundleSpecSourcesSecret(name=_WORKLOAD_CA_SECRET, key="tls.crt"))],
        target=BundleSpecTarget(
            config_map=BundleSpecTargetConfigMap(
                key=BUNDLE_KEY,
                metadata=BundleSpecTargetConfigMapMetadata(
                    annotations={"description": "The Agentplane workload CA, which client SVID verifiers trust alone"}
                ),
            )
        ),
    )


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
    add_workload_ca(chart)
    return chart
