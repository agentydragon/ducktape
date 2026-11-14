"""cert-manager serving leaves and the trust-manager workload CA for workload SVID peers.

Every Agentplane SVID comes from the workload CA, and both directions verify against it alone
(`cluster_ca.WORKLOAD_BUNDLE`): a listener checking a client SVID, and a client checking a service's
server SVID. A `cluster-internal-ca` leaf naming an Agentplane URI therefore authenticates to neither.
"""

from pathlib import Path

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, Container, Secret, Volume
from cert_manager_crds.io.cert_manager import CertificateSpecPrivateKeyRotationPolicy, CertificateSpecUsages
from constructs import Construct

from agentplane.net.peertls.config import TLS, ClientTLS
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey

_CERT_PATH = "/run/agentplane/workload-tls"
_TRUST_PATH = "/run/agentplane/workload-trust"
TRUST_BUNDLE_FILE = f"{_TRUST_PATH}/{cluster_ca.BUNDLE_KEY}"


def mount_trust_bundle(scope: Construct, container: Container) -> Path:
    """Mount the workload CA for a caller that verifies service SVIDs or a listener verifying client SVIDs."""
    trust = Volume.from_config_map(
        scope,
        "workload-trust-volume",
        ConfigMap.from_config_map_name(scope, "workload-trust-bundle", cluster_ca.WORKLOAD_BUNDLE),
        name="workload-trust",
    )
    container.mount(_TRUST_PATH, trust, read_only=True)
    return Path(TRUST_BUNDLE_FILE)


def client_tls(server_uri: str) -> ClientTLS:
    """Build the server-verifying client settings that use the shared trust mount."""
    return ClientTLS(ca_file=Path(TRUST_BUNDLE_FILE), server_uri=server_uri)


def service_certificate(scope: Construct, env: Environment, *, container: Container, name: str, uri: str) -> TLS:
    """Mount the service's client-and-server SVID and workload-CA trust bundle."""
    secret_name = f"{name}-workload-tls"
    Certificate(
        scope,
        "workload-certificate",
        metadata=ApiObjectMetadata(name=f"{name}-workload", namespace=env.namespace),
        secret_name=secret_name,
        uris=[uri],
        duration="24h",
        renew_before="8h",
        private_key=CertificatePrivateKey.ecdsa_p256(CertificateSpecPrivateKeyRotationPolicy.ALWAYS),
        usages=[
            CertificateSpecUsages.DIGITAL_SIGNATURE,
            CertificateSpecUsages.CLIENT_AUTH,
            CertificateSpecUsages.SERVER_AUTH,
        ],
        issuer_ref=cluster_ca.WORKLOAD_ISSUER,
    )
    certificate = Volume.from_secret(
        scope,
        "workload-tls-volume",
        Secret.from_secret_name(scope, "workload-tls-secret", secret_name),
        name="workload-tls",
    )
    container.mount(_CERT_PATH, certificate, read_only=True)
    ca_file = mount_trust_bundle(scope, container)
    return TLS(cert_file=f"{_CERT_PATH}/tls.crt", key_file=f"{_CERT_PATH}/tls.key", ca_file=str(ca_file))


def client_certificate(scope: Construct, env: Environment, *, container: Container, name: str, uri: str) -> TLS:
    """Mount a service client SVID and workload-CA trust bundle for a direct mTLS connection."""
    cert_path = f"/run/agentplane/{name}-client-tls"
    secret_name = f"{name}-client-workload-tls"
    Certificate(
        scope,
        f"{name}-client-workload-certificate",
        metadata=ApiObjectMetadata(name=f"{name}-client-workload", namespace=env.namespace),
        secret_name=secret_name,
        uris=[uri],
        duration="24h",
        renew_before="8h",
        private_key=CertificatePrivateKey.ecdsa_p256(CertificateSpecPrivateKeyRotationPolicy.ALWAYS),
        usages=[
            CertificateSpecUsages.DIGITAL_SIGNATURE,
            CertificateSpecUsages.CLIENT_AUTH,
            CertificateSpecUsages.SERVER_AUTH,
        ],
        issuer_ref=cluster_ca.WORKLOAD_ISSUER,
    )
    certificate = Volume.from_secret(
        scope,
        f"{name}-client-workload-tls-volume",
        Secret.from_secret_name(scope, f"{name}-client-workload-tls-secret", secret_name),
        name=f"{name}-client-workload-tls",
    )
    container.mount(cert_path, certificate, read_only=True)
    return TLS(cert_file=f"{cert_path}/tls.crt", key_file=f"{cert_path}/tls.key", ca_file=TRUST_BUNDLE_FILE)
