"""The dedicated Kubernetes API authorization proxy for Haku Agents: fail-closed, it
authenticates every request synchronously with the console, strips the Agent bearer, and
reaches kube-apiserver only with its own rotating projected ServiceAccount token.

Two listeners, two trust models. The Gateway terminates the public wildcard certificate and
forwards plain HTTP to the :8080 listener; that backend hop trusts the cluster network and
the Gateway. In-cluster kubeconfig callers (Console-launched runners and the haku-sandbox
exec pool) use the :8443 TLS listener, because client-go attaches kubeconfig credentials only
to an https server -- against plain HTTP kubectl sends every request unauthenticated.
"""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    Capability,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    PercentOrAbsolute,
    Pods,
    PodSecurityContextProps,
    Protocol,
    Secret,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
)
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
)
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.haku import console
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe

NAME = "haku-kube-api-proxy"
HOSTNAME = "haku-kubeapi.allegedly.works"
_IMAGE = "git.allegedly.works/ducktape-ci/haku-kube-api-proxy"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_HTTP_PORT = 8080
_TLS_PORT = 8443
_TLS_SECRET = "haku-kube-api-proxy-tls"
_TLS_DIR = "/etc/haku-kube-api-proxy-tls"
LABELS = {"app.kubernetes.io/name": NAME}
_SERVICE_FQDN = f"{NAME}.{console.NAMESPACE}.svc.cluster.local"


class KubeApiProxy(Construct):
    """Certificate, ServiceAccount, Deployment, Service, HTTPRoute and CiliumNetworkPolicy."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        namespace = console.NAMESPACE
        Certificate(
            self,
            "certificate",
            metadata=metadata(_TLS_SECRET, namespace),
            spec=CertificateSpec(
                secret_name=_TLS_SECRET,
                duration="2160h",
                renew_before="720h",
                private_key=CertificateSpecPrivateKey(algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA, size=256),
                common_name=_SERVICE_FQDN,
                dns_names=[_SERVICE_FQDN, f"{NAME}.{namespace}.svc"],
                # Every sandbox trust bundle already carries cluster-root-ca, so kubeconfigs
                # verify this leaf via their existing bundle.
                issuer_ref=CertificateSpecIssuerRef(name="cluster-internal-ca", kind="ClusterIssuer"),
            ),
        )
        # Execution identity for the proxy. Its projected token is rotated by Kubernetes and
        # never forwarded to, mounted into, or otherwise exposed to an Agent.
        service_account = ServiceAccount(
            self,
            "serviceaccount",
            metadata=metadata(
                NAME,
                namespace,
                annotations={
                    "description": "Executes only Kubernetes requests authorized synchronously by Haku Console."
                },
            ),
            automount_token=True,
        )
        self._add_deployment(service_account)
        Service(
            self,
            "service",
            metadata=metadata(NAME, namespace, labels=LABELS),
            selector=Pods.select(self, "pods", labels=LABELS),
            ports=[
                ServicePort(name="http", port=_HTTP_PORT, target_port=_HTTP_PORT, protocol=Protocol.TCP),
                ServicePort(name="https", port=_TLS_PORT, target_port=_TLS_PORT, protocol=Protocol.TCP),
            ],
        )
        # The proxy bounds ordinary requests to 30s itself and continuously reauthorizes
        # streams; the route stays alive long enough for practical exec and port-forward
        # sessions.
        https_route(
            self,
            "httproute",
            metadata=metadata(
                "haku-kubeapi-allegedly-works",
                namespace,
                annotations={
                    "description": "Dedicated TLS-terminated Kubernetes API route for Haku-authorized Agent traffic."
                },
            ),
            hostname=HOSTNAME,
            backend=NAME,
            port=_HTTP_PORT,
            timeout="3600s",
            hsts=False,
        )
        self._add_network_policy()

    def _add_deployment(self, service_account: ServiceAccount) -> None:
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                NAME,
                console.NAMESPACE,
                labels=LABELS,
                annotations={
                    "description": "Fail-closed Haku Agent Kubernetes authorization boundary.",
                    # cert-manager rotates the TLS Secret; the listener loads it once at start.
                    "reloader.stakater.com/auto": "true",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=LABELS),
            select=False,
            replicas=2,
            strategy=DeploymentStrategy.rolling_update(
                max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
            ),
            service_account=service_account,
            automount_service_account_token=True,
            enable_service_links=True,
            termination_grace_period=Duration.seconds(20),
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
        )
        deployment.select(LabelSelector.of(labels=LABELS))
        container = deployment.add_container(
            name="proxy",
            image=f"{_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            # haku/kube_api_proxy/cmd/main.go's environment. Public TLS keeps the original Agent
            # bearer encrypted on the proxy-to-Console hop; redirects are rejected and any
            # timeout or malformed response fails the request closed. Active exec and
            # port-forward streams fail closed within the revalidation interval plus the
            # authorization timeout after release or revocation.
            env_variables={
                "HAKU_KUBE_AUTHORIZATION_URL": EnvValue.from_value(
                    f"{console.PUBLIC_BASE_URL}/api/internal/kubernetes/authorize"
                ),
                "HAKU_KUBE_AUTHORIZATION_TIMEOUT": EnvValue.from_value("3s"),
                "HAKU_KUBE_REQUEST_TIMEOUT": EnvValue.from_value("30s"),
                "HAKU_KUBE_STREAM_REVALIDATION_INTERVAL": EnvValue.from_value("5s"),
                "HAKU_KUBE_MAX_REQUEST_BYTES": EnvValue.from_value("10485760"),
                "HAKU_KUBE_TLS_CERT_FILE": EnvValue.from_value(f"{_TLS_DIR}/tls.crt"),
                "HAKU_KUBE_TLS_KEY_FILE": EnvValue.from_value(f"{_TLS_DIR}/tls.key"),
            },
            ports=[
                ContainerPort(name="http", number=_HTTP_PORT, protocol=Protocol.TCP),
                ContainerPort(name="https", number=_TLS_PORT, protocol=Protocol.TCP),
            ],
            readiness=http_probe(
                "/healthz",
                port=_HTTP_PORT,
                initial_delay_seconds=0,
                period_seconds=5,
                timeout_seconds=2,
                failure_threshold=3,
            ),
            liveness=http_probe(
                "/healthz",
                port=_HTTP_PORT,
                initial_delay_seconds=0,
                period_seconds=10,
                timeout_seconds=2,
                failure_threshold=3,
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(25), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(32), limit=Size.mebibytes(128)),
            ),
            security_context=ContainerSecurityContextProps(
                allow_privilege_escalation=False,
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                read_only_root_filesystem=True,
            ),
        )
        tls = Secret.from_secret_name(self, "tls-secret", _TLS_SECRET)
        container.mount(_TLS_DIR, Volume.from_secret(self, "tls-volume", tls), read_only=True)
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())

    def _add_network_policy(self) -> None:
        # Selecting the proxy makes both directions default-deny: ingress from the Gateway on
        # the plaintext port and from the haku-sandbox exec pool on the TLS port (its
        # kubeconfigs authenticate, and it mounts no ServiceAccount token, so this is its only
        # kubectl path); egress to DNS for the console's name only, kube-apiserver, and the
        # public-TLS console authorization endpoint.
        cilium.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(NAME, console.NAMESPACE),
            selector=LABELS,
            ingress=[
                cilium.ingress_from_gateway(_HTTP_PORT),
                cilium.ingress_from(
                    {"k8s:io.kubernetes.pod.namespace": "haku-sandbox", "k8s:app.kubernetes.io/name": "haku-sandbox"},
                    ports=[_TLS_PORT],
                ),
            ],
            egress=[
                cilium.dns_egress(protocols=("ANY",), resolves=[console.HOSTNAME]),
                cilium.egress_to_entities("kube-apiserver", ports=[443, 6443]),
                # The console's public origin resolves to Gateway node addresses; the process
                # is configured with exactly one authorization URL and rejects redirects.
                cilium.egress_via_gateway(console.HOSTNAME),
            ],
        )
