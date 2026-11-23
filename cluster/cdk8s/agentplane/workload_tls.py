"""cert-manager serving leaves and the trust-manager workload CA for workload SVID peers.

Every Agentplane SVID comes from the workload CA, and both directions verify against it alone
(`cluster_ca.WORKLOAD_BUNDLE`): a listener checking a client SVID, and a client checking a service's
server SVID. A `cluster-internal-ca` leaf naming an Agentplane URI therefore authenticates to neither.
"""

from pathlib import Path

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, Container, Secret, Volume
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from constructs import Construct

from agentplane.identity.auth import listener
from agentplane.net.peertls.config import TLS, ClientTLS
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.providers.cert_manager.certificate import CertificatePrivateKey

_CERT_PATH = "/run/agentplane/workload-tls"
_TRUST_PATH = "/run/agentplane/workload-trust"
CERT_FILE = f"{_CERT_PATH}/tls.crt"
KEY_FILE = f"{_CERT_PATH}/tls.key"
TRUST_BUNDLE_FILE = f"{_TRUST_PATH}/{cluster_ca.BUNDLE_KEY}"


class MountedCertificate(TLS):
    """The file-backed form of peer TLS used by these Deployments."""

    cert_file: str
    key_file: str
    ca_file: str

    def spiffe_listener(self, port: int) -> listener.Listener:
        return listener.Listener(
            listen_address=f"0.0.0.0:{port}",
            tls=listener.TLS(cert_file=Path(self.cert_file), key_file=Path(self.key_file)),
            auth=[listener.SPIFFEAuth(ca_file=Path(self.ca_file))],
        )

    def client_tls(self, server_uri: str, *, client_uri: str) -> ClientTLS:
        return ClientTLS(
            ca_file=Path(self.ca_file),
            server_uri=server_uri,
            cert_file=Path(self.cert_file),
            key_file=Path(self.key_file),
            client_uri=client_uri,
        )


_SERVICE_CERTIFICATE = MountedCertificate(cert_file=CERT_FILE, key_file=KEY_FILE, ca_file=TRUST_BUNDLE_FILE)


def mount_trust_bundle(scope: Construct, container: Container) -> None:
    """Mount the workload CA for a caller that verifies service SVIDs or a listener verifying client SVIDs."""
    trust = Volume.from_config_map(
        scope,
        "workload-trust-volume",
        ConfigMap.from_config_map_name(scope, "workload-trust-bundle", cluster_ca.WORKLOAD_BUNDLE),
        name="workload-trust",
    )
    container.mount(_TRUST_PATH, trust, read_only=True)


def service_client_tls(server_uri: str, *, client_uri: str) -> ClientTLS:
    """Use the service SVID mounted by `service_certificate` and verify an exact receiver."""
    return _SERVICE_CERTIFICATE.client_tls(server_uri, client_uri=client_uri)


def certificate(scope: Construct, id: str, env: Environment, *, name: str, secret_name: str, uri: str) -> Certificate:
    return Certificate(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=env.namespace),
        spec=CertificateSpec(
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
        ),
    )


def service_certificate(
    scope: Construct, env: Environment, *, container: Container, name: str, uri: str
) -> MountedCertificate:
    """Mount the service's client-and-server SVID and workload-CA trust bundle."""
    secret_name = f"{name}-workload-tls"
    certificate(scope, "workload-certificate", env, name=f"{name}-workload", secret_name=secret_name, uri=uri)
    return mount_service_certificate(scope, container=container, name=name)


def mount_service_certificate(scope: Construct, *, container: Container, name: str) -> MountedCertificate:
    """Mount an existing serving leaf without creating or changing its Certificate."""
    secret_name = f"{name}-workload-tls"
    volume = Volume.from_secret(
        scope,
        "workload-tls-volume",
        Secret.from_secret_name(scope, "workload-tls-secret", secret_name),
        name="workload-tls",
    )
    container.mount(_CERT_PATH, volume, read_only=True)
    mount_trust_bundle(scope, container)
    return _SERVICE_CERTIFICATE


def mount_client_certificate(scope: Construct, *, container: Container, name: str) -> MountedCertificate:
    """Mount an existing client leaf without creating or changing its Certificate."""
    cert_path = f"/run/agentplane/{name}-client-tls"
    secret_name = f"{name}-client-workload-tls"
    volume = Volume.from_secret(
        scope,
        f"{name}-client-workload-tls-volume",
        Secret.from_secret_name(scope, f"{name}-client-workload-tls-secret", secret_name),
        name=f"{name}-client-workload-tls",
    )
    container.mount(cert_path, volume, read_only=True)
    return MountedCertificate(
        cert_file=f"{cert_path}/tls.crt", key_file=f"{cert_path}/tls.key", ca_file=TRUST_BUNDLE_FILE
    )
