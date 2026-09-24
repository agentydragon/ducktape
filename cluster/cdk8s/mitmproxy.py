"""agents-mitmproxy: the shared TLS-intercepting proxy that claude-sandbox's external egress is
forced through, its root CA and the trust bundle its clients read (trust model and CA rotation:
`cluster/cdk8s/mitmproxy.md`), and the policies around it. Its own FQDN fence is
`egress_fences.mitmproxy_cloud_api`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecSecretTemplate,
)
from cilium_clusterwide_crds.io.cilium import (
    CiliumClusterwideNetworkPolicy,
    CiliumClusterwideNetworkPolicySpec,
    CiliumClusterwideNetworkPolicySpecEgress,
    CiliumClusterwideNetworkPolicySpecEgressToEndpoints,
    CiliumClusterwideNetworkPolicySpecEgressToEntities,
    CiliumClusterwideNetworkPolicySpecEgressToPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumClusterwideNetworkPolicySpecEndpointSelector,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator,
)
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import Kustomization, KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from trust_manager_crds.io.cert_manager.trust import (
    Bundle,
    BundleSpec,
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from cluster.cdk8s import cilium, egress_fences
from cluster.cdk8s.flux import flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_namespace, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "mitmproxy"
NAMESPACE = egress_fences.MITMPROXY_NAMESPACE
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/mitmproxy"

# The namespaces whose external egress is forced through this proxy: the clusterwide policy
# selects them, the trust bundle lands in them, and the ingress policy admits them. A new one
# also needs the inject-mitmproxy Kyverno ClusterPolicy to cover it.
SANDBOX_NAMESPACES = ("claude-sandbox",)

_LABELS = {"app.kubernetes.io/name": NAME}
_CA_SECRET_NAME = "mitmproxy-ca"
_PROXY_PORT = 8080
_WEB_PORT = 8081
_NAMESPACE_NAME_LABEL = "kubernetes.io/metadata.name"


class Mitmproxy(Construct):
    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        self._add_ca()
        self._add_deployment()
        k8s.KubeService(
            self,
            "service",
            metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
            spec=k8s.ServiceSpec(
                selector=_LABELS,
                ports=[
                    k8s.ServicePort(
                        name="proxy", port=_PROXY_PORT, target_port=k8s.IntOrString.from_number(_PROXY_PORT)
                    ),
                    k8s.ServicePort(name="web", port=_WEB_PORT, target_port=k8s.IntOrString.from_number(_WEB_PORT)),
                ],
            ),
        )
        self._add_ingress_policy()
        self._add_sandbox_egress_policy()

    def _add_ca(self) -> None:
        Certificate(
            self,
            "certificate",
            metadata=metadata("mitmproxy-root-ca", NAMESPACE),
            spec=CertificateSpec(
                is_ca=True,
                common_name="mitmproxy-root-ca",
                secret_name=_CA_SECRET_NAME,
                duration="87600h",  # 10 years
                renew_before="8760h",  # 1 year
                private_key=CertificateSpecPrivateKey(algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA, size=256),
                # trust-manager reads Bundle sources from its own namespace.
                secret_template=CertificateSpecSecretTemplate(
                    annotations={
                        "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "cert-manager",
                        "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                        "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "cert-manager",
                    }
                ),
                issuer_ref=CertificateSpecIssuerRef(name="cluster-ca-bootstrap", kind="ClusterIssuer"),
            ),
        )
        Bundle(
            self,
            "trust-bundle",
            metadata=ApiObjectMetadata(name="mitmproxy-ca-cert"),
            spec=BundleSpec(
                sources=[
                    BundleSpecSources(use_default_c_as=True),
                    BundleSpecSources(secret=BundleSpecSourcesSecret(name="cluster-root-ca-secret", key="ca.crt")),
                    BundleSpecSources(secret=BundleSpecSourcesSecret(name=_CA_SECRET_NAME, key="tls.crt")),
                ],
                target=BundleSpecTarget(
                    config_map=BundleSpecTargetConfigMap(
                        key="ca-certificates.crt",
                        metadata=BundleSpecTargetConfigMapMetadata(
                            annotations={"description": "Trust bundle for mitmproxy-inspected sandbox HTTPS traffic"}
                        ),
                    ),
                    namespace_selector=BundleSpecTargetNamespaceSelector(
                        match_expressions=[
                            BundleSpecTargetNamespaceSelectorMatchExpressions(
                                key=_NAMESPACE_NAME_LABEL, operator="In", values=list(SANDBOX_NAMESPACES)
                            )
                        ]
                    ),
                ),
            ),
        )

    def _add_deployment(self) -> None:
        data_mount = k8s.VolumeMount(name="mitmproxy-data", mount_path="/mitmproxy-data")
        k8s.KubeDeployment(
            self,
            "deployment",
            metadata=k8s.ObjectMeta(
                name=NAME, namespace=NAMESPACE, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
            ),
            spec=k8s.DeploymentSpec(
                replicas=1,
                selector=k8s.LabelSelector(match_labels=_LABELS),
                template=k8s.PodTemplateSpec(
                    metadata=k8s.ObjectMeta(labels=_LABELS),
                    spec=k8s.PodSpec(
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
                                    str(_PROXY_PORT),
                                    "--web-host",
                                    "0.0.0.0",
                                    "--web-port",
                                    str(_WEB_PORT),
                                    "--set",
                                    "confdir=/mitmproxy-data",
                                ],
                                ports=[
                                    k8s.ContainerPort(name="proxy", container_port=_PROXY_PORT),
                                    k8s.ContainerPort(name="web", container_port=_WEB_PORT),
                                ],
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
                pod_selector=k8s.LabelSelector(match_labels=_LABELS),
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
                        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_PROXY_PORT), protocol="TCP")],
                    ),
                    k8s.NetworkPolicyIngressRule(
                        from_=[
                            k8s.NetworkPolicyPeer(
                                namespace_selector=k8s.LabelSelector(match_labels={_NAMESPACE_NAME_LABEL: "authentik"})
                            )
                        ],
                        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_WEB_PORT), protocol="TCP")],
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

        def port(
            number: int,
            protocol: CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol = (
                CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol.TCP
            ),
        ) -> CiliumClusterwideNetworkPolicySpecEgressToPortsPorts:
            return CiliumClusterwideNetworkPolicySpecEgressToPortsPorts(port=str(number), protocol=protocol)

        CiliumClusterwideNetworkPolicy(
            self,
            "sandbox-egress",
            metadata=ApiObjectMetadata(name="sandbox-force-proxy-egress"),
            spec=CiliumClusterwideNetworkPolicySpec(
                endpoint_selector=CiliumClusterwideNetworkPolicySpecEndpointSelector(
                    match_expressions=[
                        CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions(
                            key="k8s:io.kubernetes.pod.namespace",
                            operator=CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator.IN,
                            values=list(SANDBOX_NAMESPACES),
                        )
                    ]
                ),
                egress=[
                    CiliumClusterwideNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumClusterwideNetworkPolicySpecEgressToEndpoints(match_labels=cilium.KUBE_DNS_LABELS)
                        ],
                        to_ports=[
                            CiliumClusterwideNetworkPolicySpecEgressToPorts(
                                ports=[
                                    port(53, CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol.UDP),
                                    port(53),
                                ]
                            )
                        ],
                    ),
                    # Pod-to-service traffic, which bypasses the proxy via NO_PROXY.
                    CiliumClusterwideNetworkPolicySpecEgress(
                        to_entities=[CiliumClusterwideNetworkPolicySpecEgressToEntities.CLUSTER]
                    ),
                    CiliumClusterwideNetworkPolicySpecEgress(
                        to_entities=[CiliumClusterwideNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER],
                        to_ports=[CiliumClusterwideNetworkPolicySpecEgressToPorts(ports=[port(6443)])],
                    ),
                    # All external internet traffic goes through here.
                    CiliumClusterwideNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumClusterwideNetworkPolicySpecEgressToEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": NAMESPACE,
                                    "k8s:app.kubernetes.io/name": NAME,
                                }
                            )
                        ],
                        to_ports=[CiliumClusterwideNetworkPolicySpecEgressToPorts(ports=[port(_PROXY_PORT)])],
                    ),
                ],
            ),
        )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Mitmproxy(chart, NAME)
    return chart


def agents_mitmproxy(
    flux_chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, root: Path, cert_manager_trust: Kustomization
) -> Kustomization:
    """Write the directory and return its Flux node."""
    write_namespace(
        root,
        OUTPUT_DIR,
        name=NAMESPACE,
        labels={
            "goldilocks.fairwinds.com/enabled": "true",
            "goldilocks.fairwinds.com/vpa-update-mode": "auto",
            "name": NAMESPACE,
        },
    )
    write_charts(root, OUTPUT_DIR, egress_fences.mitmproxy_cloud_api, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["namespace.k8s.yaml", f"{NAME}.k8s.yaml", "cnp-cloud-api-egress.k8s.yaml"]),
    )
    return flux_kustomization(
        flux_chart,
        "agents-mitmproxy",
        artifact,
        retry_interval=None,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="5m",
        # Installs Bundle CRDs and transitively the Certificate CRDs.
        depends_on=[flux_kustomization_depends_on(cert_manager_trust)],
    )
