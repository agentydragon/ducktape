"""cert-manager serving leaves and trust-manager roots for workload SVID listeners."""

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, Container, Secret, Volume
from cert_manager_crds.io.cert_manager import (
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from constructs import Construct

from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.runner_gateway import RUNNER_CERTIFICATE_ISSUER
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, ServiceRef

_CERT_PATH = "/run/agentplane/workload-tls"
_TRUST_PATH = "/run/agentplane/workload-trust"


def serving_certificate(scope: Construct, env: Environment, *, container: Container, name: str, uri: str) -> TLS:
    """Mount a serving leaf and the existing cluster trust-manager bundle."""
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
        issuer_ref=CertificateSpecIssuerRef(name=RUNNER_CERTIFICATE_ISSUER, kind="Issuer"),
    )
    certificate = Volume.from_secret(
        scope,
        "workload-tls-volume",
        Secret.from_secret_name(scope, "workload-tls-secret", secret_name),
        name="workload-tls",
    )
    trust = Volume.from_config_map(
        scope,
        "workload-trust-volume",
        ConfigMap.from_config_map_name(scope, "workload-trust-bundle", "cluster-internal-ca-bundle"),
        name="workload-trust",
    )
    container.mount(_CERT_PATH, certificate, read_only=True)
    container.mount(_TRUST_PATH, trust, read_only=True)
    return TLS(
        cert_file=f"{_CERT_PATH}/tls.crt",
        key_file=f"{_CERT_PATH}/tls.key",
        ca_file=f"{_TRUST_PATH}/ca-certificates.crt",
    )


def admit_forwarders(scope: Construct, *, name: str, endpoint: ServiceRef, forwarders: Sequence[Pods]) -> None:
    """Admit only the new workload port from the service's existing forwarders."""
    for index, pods in enumerate(forwarders):
        NetworkPolicy(
            scope,
            f"workload-forwarder-{index}",
            metadata=ApiObjectMetadata(name=f"{name}-workload-forwarder-{index}", namespace=pods.namespace),
            endpoint_selector=pods.selector,
            egress=[endpoint.egress()],
        )
