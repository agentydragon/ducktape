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

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    Capability,
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
    Secret,
    Service,
    ServiceAccount,
    Volume,
)
from cert_manager_crds.io.cert_manager import CertificateSpecIssuerRef
from constructs import Construct

from cluster.cdk8s import cilium, pod_policy, service_ref
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.haku import console
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy

NAME = "haku-kube-api-proxy"
HOSTNAME = "haku-kubeapi.allegedly.works"
_IMAGE = "git.allegedly.works/ducktape-ci/haku-kube-api-proxy"
_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_TLS_SECRET = "haku-kube-api-proxy-tls"
_TLS_DIR = "/etc/haku-kube-api-proxy-tls"
_HTTP = service_ref.ServiceRef(
    name=NAME,
    port=service_ref.Port(name="http", number=8080),
    pods=service_ref.Pods(namespace=console.NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
_TLS = service_ref.ServiceRef(name=_HTTP.name, port=service_ref.Port(name="https", number=8443), pods=_HTTP.pods)
# The FQDN: haku-sandbox's NO_PROXY exempts the proxy by this exact name.
URL = f"https://{_TLS.fqdn}:{_TLS.port.number}"


class KubeApiProxy(Construct):
    """Certificate, ServiceAccount, Deployment, Service, HTTPRoute and CiliumNetworkPolicy."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        namespace = console.NAMESPACE
        Certificate(
            self,
            "certificate",
            metadata=ApiObjectMetadata(name=_TLS_SECRET, namespace=namespace),
            secret_name=_TLS_SECRET,
            duration="2160h",
            renew_before="720h",
            private_key=CertificatePrivateKey.ecdsa_p256(),
            common_name=_TLS.fqdn,
            dns_names=[_TLS.fqdn, _TLS.host],
            # Every sandbox trust bundle already carries cluster-root-ca, so kubeconfigs
            # verify this leaf via their existing bundle.
            issuer_ref=CertificateSpecIssuerRef(name="cluster-internal-ca", kind="ClusterIssuer"),
        )
        # Execution identity for the proxy. Its projected token is rotated by Kubernetes and
        # never forwarded to, mounted into, or otherwise exposed to an Agent.
        service_account = ServiceAccount(
            self,
            "serviceaccount",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=namespace,
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
            metadata=ApiObjectMetadata(name=_HTTP.name, namespace=namespace, labels=_HTTP.labels),
            selector=Pods.select(self, "pods", labels=_HTTP.pods.selector),
            ports=[_HTTP.port.service_port(), _TLS.port.service_port()],
        )
        # The proxy bounds ordinary requests to 30s itself and continuously reauthorizes
        # streams; the route stays alive long enough for practical exec and port-forward
        # sessions.
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(
                name="haku-kubeapi-allegedly-works",
                namespace=namespace,
                annotations={
                    "description": "Dedicated TLS-terminated Kubernetes API route for Haku-authorized Agent traffic."
                },
            ),
            hostnames=[HOSTNAME],
            backend=_HTTP,
            timeout="3600s",
            hsts=False,
        )
        self._add_network_policy()

    def _add_deployment(self, service_account: ServiceAccount) -> None:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=console.NAMESPACE,
                labels=_HTTP.pods.selector,
                annotations={"description": "Fail-closed Haku Agent Kubernetes authorization boundary."},
            ),
            pod_metadata=ApiObjectMetadata(labels=_HTTP.pods.selector),
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
        deployment.select(LabelSelector.of(labels=_HTTP.pods.selector))
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
            ports=[_HTTP.port.container_port(), _TLS.port.container_port()],
            readiness=http_probe(
                "/healthz",
                port=_HTTP.pod_port,
                initial_delay_seconds=0,
                period_seconds=5,
                timeout_seconds=2,
                failure_threshold=3,
            ),
            liveness=http_probe(
                "/healthz",
                port=_HTTP.pod_port,
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
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]), read_only_root_filesystem=True
            ),
        )
        # cert-manager rotates the TLS Secret; the listener loads it once at start, so Reloader's
        # `autoReloadAll` rolls the pods on rotation.
        tls = Secret.from_secret_name(self, "tls-secret", _TLS_SECRET)
        container.mount(_TLS_DIR, Volume.from_secret(self, "tls-volume", tls), read_only=True)
        pod_policy.harden(deployment)

    def _add_network_policy(self) -> None:
        # Selecting the proxy makes both directions default-deny: ingress from the Gateway on
        # the plaintext port and from the haku-sandbox exec pool on the TLS port (its
        # kubeconfigs authenticate, and it mounts no ServiceAccount token, so this is its only
        # kubectl path); egress to DNS for the console's name only, kube-apiserver, and the
        # public-TLS console authorization endpoint.
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=console.NAMESPACE),
            endpoint_selector=_HTTP.pods.selector,
            ingress=[
                IngressRule.from_gateway(_HTTP.pod_port),
                IngressRule.from_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": "haku-sandbox", "k8s:app.kubernetes.io/name": "haku-sandbox"},
                    ports=[_TLS.pod_port],
                ),
            ],
            egress=[
                cilium.dns_egress(protocols=("ANY",), resolves=[console.HOSTNAME]),
                EgressRule.to_entities(Entity.KUBE_APISERVER, ports=[443, 6443]),
                # The console's public origin resolves to Gateway node addresses; the process
                # is configured with exactly one authorization URL and rejects redirects.
                cilium.egress_via_gateway(console.HOSTNAME),
            ],
        )
