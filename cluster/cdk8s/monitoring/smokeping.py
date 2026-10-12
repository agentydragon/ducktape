"""smokeping_prober on `optiplex`: continuous ICMP loss and RTT from the home LAN to the AT&T
gateway, the switch, and one OVH node over Nebula.

The switch is the control. Loss to the gateway with none to the switch is the uplink fault in
debug/home_network_connectivity/2026_10_gateway_uplink_loss.md; loss only to the OVH node is
past the gateway.
"""

from __future__ import annotations

from textwrap import dedent

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecPodMetricsEndpointsMetricRelabelings,
    PodMonitorSpecSelector,
)

from cluster.cdk8s import pod_policy
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.home_lan import HOME_LAN
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.node_scheduling import OPTIPLEX
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import PodMonitor
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.service_ref import Port
from cluster.scripts.nebula_mesh import Mesh

NAME = "smokeping"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/{NAME}"
_LABELS = {"app.kubernetes.io/name": NAME}
_JOB = f"{NAMESPACE}/{NAME}"
_HTTP = Port(name="http", number=9374)
_IMAGE = (
    "quay.io/superq/smokeping-prober:v0.12.0@sha256:2524b895bdae244984e937ecb8fdd755baedbf2bb108204bd1a1dc6732706303"
)
# The image sets no USER.
_UID = 65532
# The uplink fault blacks out 200-300 ms of every second, so a 1 s interval can step over it.
_PING_INTERVAL = "200ms"
_GATEWAY = "gateway"
_SWITCH = "switch"
# Reached across the WAN; any always-on OVH node does.
_OVH_NODE = "ovh-ns104952"


def _targets(mesh: Mesh) -> dict[str, str]:
    """The `target` label each pinged address gets."""
    return {_GATEWAY: str(HOME_LAN.gateway), _SWITCH: str(HOME_LAN.switch), _OVH_NODE: mesh.hosts[_OVH_NODE].nebula_ip}


def _deployment(chart: Chart, addresses: list[str]) -> k8s.KubeDeployment:
    tcp = k8s.Probe(tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(_HTTP.name)), period_seconds=30)
    return k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            annotations={"description": "ICMP loss and RTT from the home LAN to the gateway, switch and OVH."},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            revision_history_limit=2,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    termination_grace_period_seconds=10,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=_UID,
                        run_as_group=_UID,
                        # Unprivileged ICMP (datagram) sockets, for the GIDs in this range: no
                        # root or NET_RAW. A namespaced sysctl Kubernetes allows by default.
                        sysctls=[k8s.Sysctl(name="net.ipv4.ping_group_range", value=f"{_UID} {_UID}")],
                    ),
                    containers=[
                        k8s.Container(
                            name="prober",
                            image=_IMAGE,
                            image_pull_policy="IfNotPresent",
                            args=["--no-privileged", f"--ping.interval={_PING_INTERVAL}", *addresses],
                            ports=[_HTTP.k8s_container_port()],
                            liveness_probe=tcp,
                            readiness_probe=tcp,
                            security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                            ),
                        )
                    ],
                ),
            ),
        ),
    )


def _loss(target: str) -> str:
    selector = f'job="{_JOB}", target="{target}"'
    return (
        f"(1 - sum(rate(smokeping_response_duration_seconds_count{{{selector}}}[5m]))"
        f" / sum(rate(smokeping_requests_total{{{selector}}}[5m])))"
    )


def chart(app: App, mesh: Mesh) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    targets = _targets(mesh)
    deployment = _deployment(chart, list(targets.values()))
    pod_policy.harden(deployment)
    # The gateway and switch are on optiplex's LAN, and its WAN path is the one in question.
    pod_policy.place(deployment, OPTIPLEX)
    PodMonitor(
        chart,
        "pod-monitor",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        selector=PodMonitorSpecSelector(match_labels=_LABELS),
        pod_metrics_endpoints=[
            PodMonitorSpecPodMetricsEndpoints(
                port=_HTTP.name,
                path="/metrics",
                interval="30s",
                scrape_timeout="10s",
                metric_relabelings=[
                    PodMonitorSpecPodMetricsEndpointsMetricRelabelings(
                        source_labels=["ip"], regex=address.replace(".", r"\."), target_label="target", replacement=name
                    )
                    for name, address in targets.items()
                ],
            )
        ],
    )
    PrometheusRule(
        chart,
        "prometheus-rule",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        groups=[
            group(
                NAME,
                [
                    Rule.alert(
                        "HomeGatewayUplinkLoss",
                        dedent(
                            f"""\
                            {_loss(_GATEWAY)} > 0.03
                            and on ()
                            {_loss(_SWITCH)} < 0.005
                            """
                        ),
                        for_="15m",
                        labels={"severity": "warning"},
                        summary="Packet loss from the home LAN to the AT&T gateway, none to the switch",
                        description=(
                            "optiplex has lost {{ $value | humanizePercentage }} of pings to the gateway "
                            f"({HOME_LAN.gateway}) for 15 minutes while pings to the switch ({HOME_LAN.switch}) "
                            "are clean: the switch-to-gateway uplink (CRS310 ether1) is likely back in the 2.5G "
                            "loss state. See debug/home_network_connectivity/2026_10_gateway_uplink_loss.md."
                        ),
                    )
                ],
            )
        ],
    )
    return chart


def smokeping(flux_chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        f"{NAMESPACE}-{NAME}",
        directory,
        timeout="5m",
        # the PodMonitor and PrometheusRule CRDs
        depends_on=[flux_kustomization_depends_on(monitoring_crds)],
        description="ICMP loss and RTT from the home LAN to the gateway, the switch and an OVH node.",
    )
