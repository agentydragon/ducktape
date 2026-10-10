"""mktxp, the RouterOS Prometheus exporter, for the home switch (`home_switch.py`).

Its config files mount read-only: mktxp writes one only to add a key it is missing, and ours
list every key (`_render`). ESO renders the credentials file from the `monitoring` user's
password.
"""

from __future__ import annotations

import configparser
import io
from collections.abc import Mapping
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount, k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s import pod_policy
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring.home_switch import MONITORING_PASSWORD
from cluster.cdk8s.node_scheduling import OPTIPLEX
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.providers.prometheus_operator.service_monitor import ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.bazel.runfiles import get_required_path

NAME = "mktxp"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/{NAME}"
_CONFIG_MAP = f"{NAME}-config"
_HTTP = Port(name="http", number=49090)
_SERVICE = ServiceRef(name=NAME, port=_HTTP, pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)))
_IMAGE = "ghcr.io/akpw/mktxp:2.1.0@sha256:512ebe6c83c374147ac326c57ff9225b09df777883e772d417e5275bc6820e98"
# The image's own user.
_UID = 1000
# mktxp.conf names files under these directories.
_CONFIG_DIR = "/etc/mktxp"
_CA_BUNDLE_DIR = "/etc/cluster-ca"
_CREDENTIALS_DIR = "/etc/mktxp-credentials"
_CREDENTIALS_SECRET = f"{NAME}-credentials"
_CREDENTIALS_FILE = "credentials.yaml"
# The RouterOS user tf/gitops/home-switch/main.tf creates for this exporter.
_USERNAME = "monitoring"

# Routers to scrape, each a section of `mktxp.conf` overriding its `[default]`.
_ROUTERS: Mapping[str, Mapping[str, str]] = {
    "CRS310": {
        "hostname": "192.168.1.100",
        "port": "8729",
        "credentials_file": f"{_CREDENTIALS_DIR}/{_CREDENTIALS_FILE}",
        "use_ssl": "True",
        "ssl_certificate_verify": "True",
        "ssl_check_hostname": "True",
        "ssl_ca_file": f"{_CA_BUNDLE_DIR}/{cluster_ca.BUNDLE_KEY}",
        "health": "True",
        "interface": "True",
        "monitor": "True",
        "switch_port": "True",
        "routerboard": "True",
        "installed_packages": "True",
        "neighbor": "True",
        "certificate": "True",
        "user": "True",
        # Nothing to report on a switch without routing, DHCP, PoE or wireless.
        "dhcp": "False",
        "dhcp_lease": "False",
        "connections": "False",
        "route": "False",
        "pool": "False",
        "firewall": "False",
        "public_ip": "False",
        "netwatch": "False",
        "poe": "False",
        "wireless": "False",
        "wireless_clients": "False",
        "capsman": "False",
        "capsman_clients": "False",
        "queue": "False",
    }
}
# Changes to `_mktxp.conf`, mktxp's own settings.
_SYSTEM_OVERRIDES: Mapping[str, Mapping[str, str]] = {"MKTXP": {"enable_probe": "False"}}
# The template's example router.
_SAMPLE_ROUTER = "Sample-Router"


class _ConfigParser(configparser.ConfigParser):
    def __init__(self) -> None:
        super().__init__(interpolation=None, inline_comment_prefixes=("#",), empty_lines_in_values=False)

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _render(template: str, overrides: Mapping[str, Mapping[str, str]], drop: str | None = None) -> str:
    """`template` with `overrides` set, its `drop` section removed, and its comments gone.

    mktxp rewrites a config file missing any key its template has, so ours are the template
    plus our values rather than our values alone.
    """
    config = _ConfigParser()
    config.read_string(template)
    if drop is not None:
        config.remove_section(drop)
    config.read_dict(overrides)
    out = io.StringIO()
    config.write(out)
    # `write` ends each section with a blank line, which end-of-file-fixer strips.
    return out.getvalue().rstrip("\n") + "\n"


def write_config_maps(root: Path) -> list[ConfigMapArgs]:
    """Write mktxp's config files into `OUTPUT_DIR`; return their `configMapGenerator` entry."""
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # MODULE.bazel pins these templates to the mktxp release in `_IMAGE`.
    files = {
        "mktxp.conf": ("mktxp_conf", _ROUTERS, _SAMPLE_ROUTER),
        "_mktxp.conf": ("mktxp_system_conf", _SYSTEM_OVERRIDES, None),
    }
    for name, (repository, overrides, drop) in files.items():
        template = get_required_path(f"{repository}/file/{name}").read_text()
        (out_dir / name).write_text(_render(template, overrides, drop))
    return [ConfigMapArgs(name=_CONFIG_MAP, namespace=NAMESPACE, files=list(files))]


def _deployment(chart: Chart) -> k8s.KubeDeployment:
    labels = _SERVICE.pods.selector
    tcp = k8s.Probe(tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(_HTTP.name)), period_seconds=30)
    return k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            labels=labels,
            annotations={"description": "Prometheus metrics read from the home MikroTik switch's RouterOS API."},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            revision_history_limit=2,
            selector=k8s.LabelSelector(match_labels=labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    termination_grace_period_seconds=10,
                    security_context=k8s.PodSecurityContext(run_as_non_root=True, run_as_user=_UID, run_as_group=_UID),
                    containers=[
                        k8s.Container(
                            name="exporter",
                            image=_IMAGE,
                            image_pull_policy="IfNotPresent",
                            command=["mktxp", "--cfg-dir", _CONFIG_DIR, "export"],
                            ports=[_HTTP.k8s_container_port()],
                            liveness_probe=tcp,
                            readiness_probe=tcp,
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("200m"),
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                },
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR, read_only=True),
                                k8s.VolumeMount(name="credentials", mount_path=_CREDENTIALS_DIR, read_only=True),
                                k8s.VolumeMount(name="cluster-ca", mount_path=_CA_BUNDLE_DIR, read_only=True),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP)),
                        k8s.Volume(name="credentials", secret=k8s.SecretVolumeSource(secret_name=_CREDENTIALS_SECRET)),
                        k8s.Volume(name="cluster-ca", config_map=k8s.ConfigMapVolumeSource(name=cluster_ca.BUNDLE)),
                    ],
                ),
            ),
        ),
    )


def _credentials(chart: Chart) -> None:
    reader = ServiceAccount(
        chart, "credentials-reader", metadata=ApiObjectMetadata(name=f"{NAME}-credentials-reader", namespace=NAMESPACE)
    )
    store = single_secret_store(
        chart,
        _CREDENTIALS_SECRET,
        reader=reader,
        source_namespace=MONITORING_PASSWORD.secret.namespace,
        source_secret=MONITORING_PASSWORD.secret.name,
        consumer_namespace=NAMESPACE,
    )
    ExternalSecret(
        chart,
        "credentials",
        metadata=ApiObjectMetadata(name=_CREDENTIALS_SECRET, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(store),
        data=[remote_data(MONITORING_PASSWORD.secret.name, MONITORING_PASSWORD.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            data={_CREDENTIALS_FILE: f"username: {_USERNAME}\npassword: {{{{ .{MONITORING_PASSWORD.key} }}}}\n"}
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _credentials(chart)
    deployment = _deployment(chart)
    pod_policy.harden(deployment)
    # The switch's management services accept only the home LAN.
    pod_policy.place(deployment, OPTIPLEX)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=_SERVICE.labels),
        spec=k8s.ServiceSpec(selector=_SERVICE.pods.selector, ports=[_HTTP.k8s_service_port()]),
    )
    ServiceMonitor(
        chart,
        "monitor",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_SERVICE.labels),
        endpoints=[
            ServiceMonitorSpecEndpoints(
                port=_HTTP.name,
                path="/metrics",
                scheme=ServiceMonitorSpecEndpointsScheme.HTTP,
                interval="60s",
                # mktxp queries the switch during the scrape.
                scrape_timeout="30s",
            )
        ],
    )
    add_fleet_rules(chart)
    return chart


def mktxp(
    flux_chart: Chart,
    directory: RenderedDirectory,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
    home_switch: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        f"{NAMESPACE}-{NAME}",
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            # ExternalSecret, ClusterSecretStore
            external_secrets_operator,
            # ServiceMonitor
            monitoring_crds,
            # The `monitoring` user's password
            home_switch,
        ),
        description="Home MikroTik switch metrics: per-port traffic, errors, link state, health.",
    )
