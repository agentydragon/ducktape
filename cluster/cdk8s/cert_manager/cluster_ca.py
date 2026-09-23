"""The cluster's self-signed root CA: the bootstrap ClusterIssuer that signs it, its
Certificate, the `cluster-internal-ca` ClusterIssuer that issues from it, and the
trust-manager Bundle that distributes it, with the active Let's Encrypt root, to every
namespace.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cert_manager_clusterissuer_crds.io.cert_manager import (
    ClusterIssuer,
    ClusterIssuerSpec,
    ClusterIssuerSpecCa,
    ClusterIssuerSpecSelfSigned,
)
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
)
from trust_manager_crds.io.cert_manager.trust import (
    Bundle,
    BundleSpec,
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
)

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "cluster-ca"
OUTPUT_DIR = "cluster/k8s/cert-manager/cluster-ca/base"
_ROOT_CA_SECRET = "cluster-root-ca-secret"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    bootstrap = ClusterIssuer(
        chart,
        "bootstrap",
        metadata=ApiObjectMetadata(name="cluster-ca-bootstrap"),
        spec=ClusterIssuerSpec(self_signed=ClusterIssuerSpecSelfSigned()),
    )
    Certificate(
        chart,
        "root-ca",
        metadata=metadata("cluster-root-ca", "cert-manager"),
        spec=CertificateSpec(
            is_ca=True,
            common_name="cluster-root-ca",
            secret_name=_ROOT_CA_SECRET,
            duration="87600h",  # 10 years
            renew_before="8760h",  # 1 year
            private_key=CertificateSpecPrivateKey(algorithm=CertificateSpecPrivateKeyAlgorithm.RSA, size=4096),
            issuer_ref=CertificateSpecIssuerRef(name=bootstrap.name, kind="ClusterIssuer"),
        ),
    )
    # Issues internal service certificates from the root CA.
    ClusterIssuer(
        chart,
        "internal",
        metadata=ApiObjectMetadata(name="cluster-internal-ca"),
        spec=ClusterIssuerSpec(ca=ClusterIssuerSpecCa(secret_name=_ROOT_CA_SECRET)),
    )
    Bundle(
        chart,
        "bundle",
        metadata=ApiObjectMetadata(name="cluster-internal-ca-bundle"),
        spec=BundleSpec(
            sources=[
                BundleSpecSources(secret=BundleSpecSourcesSecret(name=_ROOT_CA_SECRET, key="ca.crt")),
                # The active issuer's root, from `config/base`; Flux postBuild substitutes it.
                BundleSpecSources(secret=BundleSpecSourcesSecret(name="${LETSENCRYPT_ISSUER}-root-ca", key="ca.crt")),
            ],
            target=BundleSpecTarget(config_map=BundleSpecTargetConfigMap(key="ca-certificates.crt")),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
