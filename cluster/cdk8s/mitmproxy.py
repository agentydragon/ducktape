"""agents-mitmproxy: the shared TLS-intercepting proxy that claude-sandbox's external egress is
forced through, its root CA and the trust bundle its clients read (trust model and CA rotation:
`cluster/cdk8s/mitmproxy.md`), and the policies around it. Its own FQDN fence is
`egress_fences.mitmproxy_cloud_api`.
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import cilium, egress_fences, namespaces
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "mitmproxy"
NAMESPACE = egress_fences.MITMPROXY_NAMESPACE
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/mitmproxy"

# The namespaces whose external egress is forced through this proxy: the clusterwide policy
# selects them, the trust bundle lands in them, and the ingress policy admits them. A new one
# also needs the inject-mitmproxy Kyverno ClusterPolicy to cover it.
SANDBOX_NAMESPACES = ("claude-sandbox",)

# The proxy the sandboxes' external egress is forced through.
PROXY = ServiceRef(
    name=NAME,
    port=Port(name="proxy", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
# The mitmweb UI; the hand-written authentik blueprint agents-mitmproxy-sso.yaml proxies to it.
_WEB = ServiceRef(name=PROXY.name, port=Port(name="web", number=8081), pods=PROXY.pods)
_CA_SECRET_NAME = "mitmproxy-ca"
_NAMESPACE_NAME_LABEL = "kubernetes.io/metadata.name"


class Mitmproxy(Construct):
    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        self._add_ca()
        self._add_deployment()
        k8s.KubeService(
            self,
            "service",
            metadata=k8s.ObjectMeta(name=PROXY.name, namespace=NAMESPACE),
            spec=k8s.ServiceSpec(
                selector=PROXY.pods.selector, ports=[PROXY.port.k8s_service_port(), _WEB.port.k8s_service_port()]
            ),
        )
        self._add_ingress_policy()
        self._add_sandbox_egress_policy()

    def _add_ca(self) -> None:
        interception_root_ca(
            self,
            name="mitmproxy-root-ca",
            namespace=NAMESPACE,
            secret_name=_CA_SECRET_NAME,
            bundle_name="mitmproxy-ca-cert",
            description="Trust bundle for mitmproxy-inspected sandbox HTTPS traffic",
            target_namespaces=SANDBOX_NAMESPACES,
        )

    def _add_deployment(self) -> None:
        data_mount = k8s.VolumeMount(name="mitmproxy-data", mount_path="/mitmproxy-data")
        k8s.KubeDeployment(
            self,
            "deployment",
            metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=PROXY.pods.selector),
            spec=k8s.DeploymentSpec(
                replicas=1,
                selector=k8s.LabelSelector(match_labels=PROXY.pods.selector),
                template=k8s.PodTemplateSpec(
                    metadata=k8s.ObjectMeta(labels=PROXY.pods.selector),
                    spec=k8s.PodSpec(
                        automount_service_account_token=False,
                        init_containers=[
                            k8s.Container(
                                name="mitmproxy-ca-init",
                                image="busybox:1.38",
                                command=[
                                    "sh",
                                    "-c",
                                    "cat /mitmproxy-ca/tls.key /mitmproxy-ca/tls.crt > /mitmproxy-data/mitmproxy-ca.pem\n"
                                    "cp /mitmproxy-ca/tls.crt /mitmproxy-data/mitmproxy-ca-cert.pem\n",
                                ],
                                volume_mounts=[
                                    k8s.VolumeMount(name="mitmproxy-ca", mount_path="/mitmproxy-ca", read_only=True),
                                    data_mount,
                                ],
                            )
                        ],
                        containers=[
                            k8s.Container(
                                name=NAME,
                                # >=12.2.3 (mitmproxy#8214): the leaf's AuthorityKeyIdentifier copies the
                                # CA cert's SubjectKeyIdentifier instead of recomputing it as SHA-1.
                                # cert-manager mints CA SKIs per RFC 7093 (truncated SHA-256), so on
                                # <12.2.3 every intercepted leaf's AKID mismatched the CA SKI and strict
                                # clients rejected the chain ("unable to get local issuer certificate").
                                # See cluster/docs/lessons_learned/2026_06_25_mitmproxy_ca_ski_aki_mismatch.md.
                                image="mitmproxy/mitmproxy:12.2.3",
                                command=[
                                    "mitmweb",
                                    "--listen-host",
                                    "0.0.0.0",
                                    "--listen-port",
                                    str(PROXY.pod_port),
                                    "--web-host",
                                    "0.0.0.0",
                                    "--web-port",
                                    str(_WEB.pod_port),
                                    "--set",
                                    "confdir=/mitmproxy-data",
                                ],
                                ports=[PROXY.port.k8s_container_port(), _WEB.port.k8s_container_port()],
                                volume_mounts=[data_mount],
                                resources=k8s.ResourceRequirements(
                                    requests={
                                        "cpu": k8s.Quantity.from_string("50m"),
                                        "memory": k8s.Quantity.from_string("128Mi"),
                                    },
                                    limits={
                                        "cpu": k8s.Quantity.from_string("500m"),
                                        "memory": k8s.Quantity.from_string("512Mi"),
                                    },
                                ),
                            )
                        ],
                        volumes=[
                            k8s.Volume(name="mitmproxy-ca", secret=k8s.SecretVolumeSource(secret_name=_CA_SECRET_NAME)),
                            k8s.Volume(name="mitmproxy-data", empty_dir=k8s.EmptyDirVolumeSource()),
                        ],
                    ),
                ),
            ),
        )

    def _add_ingress_policy(self) -> None:
        """Sandbox proxy clients reach the proxy port; the Authentik outpost reaches the mitmweb UI
        for proxy auth."""
        k8s.KubeNetworkPolicy(
            self,
            "ingress",
            metadata=k8s.ObjectMeta(name="allow-authentik-mitmproxy-ingress", namespace=NAMESPACE),
            spec=k8s.NetworkPolicySpec(
                pod_selector=k8s.LabelSelector(match_labels=PROXY.pods.selector),
                ingress=[
                    k8s.NetworkPolicyIngressRule(
                        from_=[
                            k8s.NetworkPolicyPeer(
                                namespace_selector=k8s.LabelSelector(
                                    match_expressions=[
                                        k8s.LabelSelectorRequirement(
                                            key=_NAMESPACE_NAME_LABEL, operator="In", values=list(SANDBOX_NAMESPACES)
                                        )
                                    ]
                                )
                            )
                        ],
                        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(PROXY.pod_port), protocol="TCP")],
                    ),
                    k8s.NetworkPolicyIngressRule(
                        from_=[
                            k8s.NetworkPolicyPeer(
                                namespace_selector=k8s.LabelSelector(match_labels={_NAMESPACE_NAME_LABEL: "authentik"})
                            )
                        ],
                        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_WEB.pod_port), protocol="TCP")],
                    ),
                ],
                policy_types=["Ingress"],
            ),
        )

    def _add_sandbox_egress_policy(self) -> None:
        """Force the sandboxes' external egress through the proxy: DNS, cluster-internal traffic,
        the kube-apiserver and the proxy port; no direct internet.

        KNOWN GAP (intentionally left open for now; may be closed later): this constrains egress
        from the sandbox *pods*, not what a sandboxed agent can launch elsewhere. Cluster-internal
        traffic is allowed, so an agent that can reach the docker-ci DinD -- and obtain its mTLS
        client cert -- can `docker run` a container there whose egress is NOT forced through
        mitmproxy, and use it to fetch/exfiltrate outside the proxy. Closing it means either
        denying sandbox pods access to docker-ci, or running agent workloads on a locked-down
        "docker-for-agents" daemon that forces every container's egress through mitmproxy. See
        also loom/gym/TODO.md (the loom eval's per-sandbox archive clamp rests on the same
        assumption).
        """
        cilium.force_proxy_egress(
            self,
            "sandbox-egress",
            name="sandbox-force-proxy-egress",
            namespaces=SANDBOX_NAMESPACES,
            proxy_namespace=PROXY.pods.namespace,
            proxy_name=NAME,
            proxy_port=PROXY.pod_port,
            # Pod-to-service traffic, which bypasses the proxy via NO_PROXY.
            cluster_ports=None,
            kube_apiserver=True,
        )


def namespace_chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO, labels={"name": NAMESPACE})
    return chart


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Mitmproxy(chart, NAME)
    return chart


def agents_mitmproxy(
    flux_chart: Chart, directory: RenderedDirectory, cert_manager_trust: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "agents-mitmproxy",
        directory,
        retry_interval=None,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="5m",
        # Installs Bundle CRDs and transitively the Certificate CRDs.
        depends_on=[flux_kustomization_depends_on(cert_manager_trust)],
    )
