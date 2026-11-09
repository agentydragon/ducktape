"""cert-manager serving leaves and trust-manager roots for workload SVID listeners.

A listener verifies client SVIDs against the Agentplane workload CA alone (`cluster_ca.WORKLOAD_BUNDLE`).
A client verifies a service's server certificate against the cluster's internal roots, which include
the workload CA.
"""

from pathlib import Path

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, Container, Secret, Volume
from cert_manager_crds.io.cert_manager import CertificateSpecPrivateKeyRotationPolicy, CertificateSpecUsages
from constructs import Construct

from agentplane.environment_runtime_transport.config import TLS, ClientTLS
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey

_CERT_PATH = "/run/agentplane/workload-tls"
_TRUST_PATH = "/run/agentplane/workload-trust"
TRUST_BUNDLE_FILE = f"{_TRUST_PATH}/ca-certificates.crt"
_CLIENT_TRUST_PATH = "/run/agentplane/workload-client-trust"
CLIENT_TRUST_BUNDLE_FILE = f"{_CLIENT_TRUST_PATH}/{cluster_ca.BUNDLE_KEY}"


def mount_trust_bundle(scope: Construct, container: Container) -> Path:
    """Mount the cluster's internal roots for a caller that verifies service SVIDs."""
    trust = Volume.from_config_map(
        scope,
        "workload-trust-volume",
        ConfigMap.from_config_map_name(scope, "workload-trust-bundle", "cluster-internal-ca-bundle"),
        name="workload-trust",
    )
    container.mount(_TRUST_PATH, trust, read_only=True)
    return Path(TRUST_BUNDLE_FILE)


def client_tls(server_uri: str) -> ClientTLS:
    """Build the server-verifying client settings that use the shared trust mount."""
    return ClientTLS(ca_file=Path(TRUST_BUNDLE_FILE), server_uri=server_uri)


def mount_client_trust_bundle(scope: Construct, container: Container) -> Path:
    """Mount the workload CA for a listener that verifies client SVIDs."""
    trust = Volume.from_config_map(
        scope,
        "workload-client-trust-volume",
        ConfigMap.from_config_map_name(scope, "workload-client-trust-bundle", cluster_ca.WORKLOAD_BUNDLE),
        name="workload-client-trust",
    )
    container.mount(_CLIENT_TRUST_PATH, trust, read_only=True)
    return Path(CLIENT_TRUST_BUNDLE_FILE)


def serving_certificate(scope: Construct, env: Environment, *, container: Container, name: str, uri: str) -> TLS:
    """Mount a serving leaf from the workload CA, the cluster roots for the workload's own clients, and
    the workload CA as `ca_file` for its listener's client SVIDs."""
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
        usages=[CertificateSpecUsages.DIGITAL_SIGNATURE, CertificateSpecUsages.SERVER_AUTH],
        issuer_ref=cluster_ca.WORKLOAD_ISSUER,
    )
    certificate = Volume.from_secret(
        scope,
        "workload-tls-volume",
        Secret.from_secret_name(scope, "workload-tls-secret", secret_name),
        name="workload-tls",
    )
    container.mount(_CERT_PATH, certificate, read_only=True)
    mount_trust_bundle(scope, container)
    ca_file = mount_client_trust_bundle(scope, container)
    return TLS(cert_file=f"{_CERT_PATH}/tls.crt", key_file=f"{_CERT_PATH}/tls.key", ca_file=str(ca_file))
