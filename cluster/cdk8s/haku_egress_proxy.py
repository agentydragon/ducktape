"""Shared Haku sandbox / CI mitmproxy, interception CA, trust bundle and egress fence.

The retired OpenClaw spike's Iron proxy is preserved in parked/haku_openclaw_spike_proxy.py.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import cilium, egress_fences
from cluster.cdk8s.agents import namespaces
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "haku-egress-proxy"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/haku-egress-proxy"
# The mitmproxy chokepoint haku-sandbox and haku-ci send their external egress through.
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="proxy", number=8080),
    pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", NAME),)),
)
_CA_SECRET = "haku-egress-proxy-ca"


def _quantities(**values: str) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _ca(chart: Chart) -> None:
    interception_root_ca(
        chart,
        name="haku-egress-proxy-root-ca",
        namespace=NAME,
        secret_name=_CA_SECRET,
        bundle_name="haku-egress-proxy-ca-cert",
        description="Trust bundle for haku-egress-proxy-inspected sandbox HTTPS traffic",
        reflection_namespaces=("cert-manager",),
        # Written into Haku trust domains. The CLIProxyAPI-backed aiquota path connects
        # directly to the in-cluster management service and does not trust or use this
        # inspected egress listener. public-coder-agent has its own separate interception CA
        # (public_coder/proxy.py) and does not consume this bundle.
        target_namespaces=("haku-sandbox", "haku-ci"),
    )


_MITMPROXY_CA_INIT_SCRIPT = """\
cat /mitmproxy-ca/tls.key /mitmproxy-ca/tls.crt > /mitmproxy-data/mitmproxy-ca.pem
cp /mitmproxy-ca/tls.crt /mitmproxy-data/mitmproxy-ca-cert.pem
"""


def _mitmproxy(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME, labels=SERVICE.pods.selector),
        spec=k8s.DeploymentSpec(
            # Two, so one container's restart never empties the Service. mitmproxy OOM-kills
            # under haku-ci traffic (#5846), and with one replica every kill was a CI outage:
            # dependency fetches mid-flight got "connection refused" for the restart's duration.
            replicas=2,
            selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    affinity=k8s.Affinity(
                        pod_anti_affinity=k8s.PodAntiAffinity(
                            # Preferred, not required: this proxy is haku-ci's only egress path, so a
                            # replica left Pending by a hard rule costs more than two on one node.
                            preferred_during_scheduling_ignored_during_execution=[
                                k8s.WeightedPodAffinityTerm(
                                    weight=100,
                                    pod_affinity_term=k8s.PodAffinityTerm(
                                        label_selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
                                        topology_key="kubernetes.io/hostname",
                                    ),
                                )
                            ]
                        )
                    ),
                    init_containers=[
                        k8s.Container(
                            name="mitmproxy-ca-init",
                            image="busybox:1.38",
                            command=["sh", "-c", _MITMPROXY_CA_INIT_SCRIPT],
                            volume_mounts=[
                                k8s.VolumeMount(name="mitmproxy-ca", mount_path="/mitmproxy-ca", read_only=True),
                                k8s.VolumeMount(name="mitmproxy-data", mount_path="/mitmproxy-data"),
                            ],
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name="mitmproxy",
                            # Egress proxy — currently implemented with mitmproxy.
                            # >=12.2.3 (mitmproxy#8214): the leaf's AuthorityKeyIdentifier now
                            # copies the CA cert's SubjectKeyIdentifier instead of recomputing it
                            # as SHA-1. cert-manager mints CA SKIs per RFC 7093 (truncated
                            # SHA-256), so on <12.2.3 every intercepted leaf's AKID mismatched the
                            # CA SKI and strict clients rejected the chain ("unable to get local
                            # issuer certificate"). See
                            # cluster/docs/lessons_learned/2026_06_25_mitmproxy_ca_ski_aki_mismatch.md.
                            image="mitmproxy/mitmproxy:12.2.3",
                            command=[
                                # mitmdump, not mitmweb. mitmweb keeps every flow in its View store
                                # for the UI, with no eviction, so under CI traffic the store grew
                                # until the container was OOM-killed (#5846). mitmdump keeps no
                                # view: it logs one line per flow to stdout and retains nothing.
                                # Nothing used the 8081 UI.
                                "mitmdump",
                                "--listen-host",
                                "0.0.0.0",
                                "--listen-port",
                                str(SERVICE.pod_port),
                                "--set",
                                "confdir=/mitmproxy-data",
                                # Stream (don't buffer) response bodies over 1 MB. dind pulls
                                # multi-hundred-MB image layers through here; buffering them
                                # OOM-killed the container (exit 137, connection refused
                                # mid-restart → haku-ci builds failed). We gate at the
                                # CONNECT/host level (the allowlist), not blob content, so
                                # streaming loses no enforcement.
                                "--set",
                                "stream_large_bodies=1m",
                                # Passthrough (raw TCP, no TLS interception) for Anthropic's control
                                # plane. The haku-managed-agent worker drives Managed Agents sessions
                                # over a long-lived HTTP/2 stream to api.anthropic.com; mitmproxy's
                                # interception buffers/breaks that stream, so the worker claims work
                                # but tool results never post and sessions deadlock at "idle". This
                                # traffic carries only the scoped environment key and needs no
                                # inspection. Egress still flows through the proxy (the CCNP
                                # chokepoint is unchanged); only TLS interception is skipped here.
                                # TODO(haku): tighten later — this passes ALL of api.anthropic.com
                                #   through untouched. Revisit whether only the session stream needs
                                #   passthrough (and whether to inspect the rest).
                                "--ignore-hosts",
                                r"api\.anthropic\.com",
                            ],
                            ports=[SERVICE.port.k8s_container_port()],
                            # An endpoint only once the listener is up: with two replicas a rolling
                            # update otherwise routes to a pod that is not listening yet.
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(SERVICE.port.name)),
                                period_seconds=5,
                            ),
                            volume_mounts=[k8s.VolumeMount(name="mitmproxy-data", mount_path="/mitmproxy-data")],
                            resources=k8s.ResourceRequirements(
                                requests=_quantities(cpu="50m", memory="256Mi"),
                                # Memory: headroom over the 512Mi that OOM-killed under haku-ci
                                # build traffic.
                                # TODO(vpa-memory-audit): 1Gi -> 3Gi. VPA observed a 1.15Gi
                                # request / 1.73Gi upper bound, and mitmproxy was OOM-killed at 3Gi
                                # again on 2026-09-08 after ~53h of traffic (#5846). The cause was
                                # mitmweb's flow store, gone since the switch to mitmdump; observe
                                # the working set under mitmdump and lower this to match.
                                limits=_quantities(cpu="500m", memory="3Gi"),
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="mitmproxy-ca", secret=k8s.SecretVolumeSource(secret_name=_CA_SECRET)),
                        k8s.Volume(name="mitmproxy-data", empty_dir=k8s.EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAME),
        spec=k8s.ServiceSpec(selector=SERVICE.pods.selector, ports=[SERVICE.port.k8s_service_port()]),
    )
    # With two replicas, a voluntary disruption (node drain, descheduler eviction, rolling
    # update) may take one proxy pod at a time but never both, so haku-ci's only egress path
    # keeps a ready endpoint throughout. The descheduler honors PDBs unconditionally.
    k8s.KubePodDisruptionBudget(
        chart,
        "poddisruptionbudget",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAME),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector)
        ),
    )
    # Allow proxy clients (haku-sandbox + haku-ci) to reach the egress proxy (port 8080) and
    # the Authentik outpost to reach the mitmweb UI (port 8081) for proxy auth. Without the
    # haku-ci ingress the runner/dind get `proxyconnect ... i/o timeout` on every egress.
    k8s.KubeNetworkPolicy(
        chart,
        "networkpolicy",
        metadata=k8s.ObjectMeta(name="allow-authentik-egress-proxy-ingress", namespace=NAME),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": namespace}
                            )
                        )
                        for namespace in ("haku-sandbox", "haku-ci")
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(SERVICE.pod_port), protocol="TCP")],
                ),
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "authentik"}
                            )
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8081), protocol="TCP")],
                ),
            ],
            policy_types=["Ingress"],
        ),
    )


def _sandbox_fence(chart: Chart) -> None:
    """Force all external egress from the haku-sandbox namespace through the dedicated
    haku-egress-proxy. Allows: DNS, cluster-internal traffic, kube-apiserver, and haku-egress-proxy
    port 8080. Blocks: direct external internet access.
    """
    cilium.force_proxy_egress(
        chart,
        "haku-sandbox-force-proxy-egress",
        name="haku-sandbox-force-proxy-egress",
        namespaces=["haku-sandbox"],
        proxy_namespace=SERVICE.pods.namespace,
        proxy_name=NAME,
        proxy_port=SERVICE.pod_port,
        # All cluster-internal traffic (pod-to-service, bypasses proxy via NO_PROXY).
        # This is also how haku-sandbox reaches the Plaid Postgres cluster-internally.
        cluster_ports=None,
        kube_apiserver=True,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAME)
    _ca(chart)
    _mitmproxy(chart)
    _sandbox_fence(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, namespaces.haku_egress_proxy, chart, egress_fences.haku_cloud_api)
