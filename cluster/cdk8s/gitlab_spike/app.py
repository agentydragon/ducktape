"""GitLab spike chart: three independent Gitaly disks behind Praefect."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cilium_envoyconfig_crds.io.cilium import (
    CiliumEnvoyConfig,
    CiliumEnvoyConfigSpec,
    CiliumEnvoyConfigSpecBackendServices,
    CiliumEnvoyConfigSpecNodeSelector,
)
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeRemediation,
)

from cluster.cdk8s import cnpg, node_scheduling
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "gitlab-spike"
NAMESPACE = NAME
OUTPUT_DIR = f"{GENERATED_ROOT}/gitlab-spike/app"
HOSTNAME = "gitlab-spike.allegedly.works"
REGISTRY_HOSTNAME = "registry-gitlab-spike.allegedly.works"
CHART_VERSION = "10.5.1"  # GitLab 19.5.1, published 2026-10-21 in charts.gitlab.io.

GITLAB_DB = cnpg.PostgresRef.generated(name="gitlab-db", namespace=NAMESPACE)
PRAEFECT_PASSWORD = SecretRef(namespace=NAMESPACE, name="praefect-db-credentials")
GITLAB_DATABASE = "gitlabhq_production"
PRAEFECT_DATABASE = "praefect"
VALKEY = "gitlab-valkey"
OIDC = SecretRef(namespace=NAMESPACE, name="gitlab-spike-oidc").key("provider")
ROOT_PASSWORD = SecretRef(namespace=NAMESPACE, name="gitlab-root-password").key("password")
BOOTSTRAP_TOKEN = SecretRef(namespace=NAMESPACE, name="gitlab-bootstrap-token").key("token")
BOOTSTRAP_CONFIG_MAP = "gitlab-spike-bootstrap"

# Separate buckets preserve GitLab's per-feature backup/restore semantics. Each has its own
# SeaweedFS identity and chart connection Secret, created in dependencies.py.
OBJECT_BUCKETS = {
    "lfs": "gitlab-spike-lfs",
    "artifacts": "gitlab-spike-artifacts",
    "uploads": "gitlab-spike-uploads",
    "packages": "gitlab-spike-packages",
}
REGISTRY_BUCKET = "gitlab-spike-registry"
BACKUP_BUCKET = "gitlab-spike-backups"
BACKUP_TMP_BUCKET = "gitlab-spike-backup-tmp"
BACKUP_S3CFG_SECRET = "gitlab-spike-backups-s3cfg"

WEB = ServiceRef(
    name=f"{NAME}-webservice-default",
    port=Port(name="http-workhorse", number=8181),
    pods=Pods(namespace=NAMESPACE, labels=(("app", "webservice"), ("release", NAME))),
)
REGISTRY = ServiceRef(
    name=f"{NAME}-registry",
    port=Port(name="http-registry", number=5000),
    pods=Pods(namespace=NAMESPACE, labels=(("app", "registry"), ("release", NAME))),
)
SSH = ServiceRef(
    name=f"{NAME}-gitlab-shell",
    # global.shell.port also sets the chart's Service port; its Pod target stays 2222.
    port=Port(name="ssh", number=2223),
    pods=Pods(namespace=NAMESPACE, labels=(("app", "gitlab-shell"), ("release", NAME))),
)


def _ssh_listener(scope: Construct) -> None:
    # Cilium's Gateway API has no TCPRoute support, so raw SSH gets its own Envoy
    # listener on the HIL gateway nodes. The backend remains a ClusterIP Service.
    service = SSH.name
    cluster = f"{NAMESPACE}:{service}:{SSH.port.number}"
    CiliumEnvoyConfig(
        scope,
        "ssh-listener",
        metadata=ApiObjectMetadata(
            name=service, namespace=NAMESPACE, annotations={"cec.cilium.io/use-original-source-address": "false"}
        ),
        spec=CiliumEnvoyConfigSpec(
            node_selector=CiliumEnvoyConfigSpecNodeSelector(match_labels={"topology.kubernetes.io/region": "hil"}),
            backend_services=[
                CiliumEnvoyConfigSpecBackendServices(name=service, namespace=NAMESPACE, number=[str(SSH.port.number)])
            ],
            resources=[
                {
                    "@type": "type.googleapis.com/envoy.config.listener.v3.Listener",
                    "name": service,
                    "address": {"socketAddress": {"address": "0.0.0.0", "portValue": 2223}},
                    "filterChains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.tcp_proxy",
                                    "typedConfig": {
                                        "@type": "type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy",
                                        "statPrefix": service,
                                        "cluster": cluster,
                                        "idleTimeout": "3600s",
                                    },
                                }
                            ]
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/envoy.config.cluster.v3.Cluster",
                    "name": cluster,
                    "type": "EDS",
                    "edsClusterConfig": {"serviceName": f"{NAMESPACE}/{service}:{SSH.port.number}"},
                    "outlierDetection": {"splitExternalLocalOriginErrors": True},
                },
            ],
        ),
    )


def _values() -> dict[str, object]:
    app_config = {
        feature: {
            "enabled": True,
            "bucket": bucket,
            "connection": {"secret": f"{bucket}-connection", "key": "connection"},
        }
        for feature, bucket in OBJECT_BUCKETS.items()
    }
    app_config["omniauth"] = {
        "enabled": True,
        # Terraform creates the user and identity before its first sign-in.
        "allowSingleSignOn": False,
        "blockAutoCreatedUsers": True,
        "providers": [{"secret": OIDC.secret.name, "key": OIDC.key}],
    }
    app_config["initialDefaults"] = {"signupEnabled": False}
    app_config["object_store"] = {"enabled": False}  # Per-feature credentials have separate IAM grants.
    app_config["backups"] = {
        "bucket": BACKUP_BUCKET,
        "tmpBucket": BACKUP_TMP_BUCKET,
        "objectStorage": {"backend": "s3"},
    }
    return {
        "global": {
            "hosts": {
                "domain": "allegedly.works",
                "gitlab": {"name": HOSTNAME},
                "registry": {"name": REGISTRY_HOSTNAME},
                "ssh": HOSTNAME,
            },
            "gatewayApi": {
                "enabled": False,
                "installEnvoy": False,
                "configureEnvoy": False,
                "configureCertmanager": False,
            },
            "ingress": {"enabled": False, "configureCertmanager": False},
            "initialRootPassword": {"secret": ROOT_PASSWORD.secret.name, "key": ROOT_PASSWORD.key},
            "shell": {"port": 2223},
            "psql": {
                "host": GITLAB_DB.rw.host,
                "port": GITLAB_DB.rw.port.number,
                "username": GITLAB_DATABASE,
                "database": GITLAB_DATABASE,
                "password": {"secret": GITLAB_DB.app_secret.name, "key": "password"},
            },
            "redis": {"host": f"{VALKEY}-master.{NAMESPACE}.svc", "port": 6379, "auth": {"enabled": False}},
            "praefect": {
                "enabled": True,
                "replaceInternalGitaly": True,
                "virtualStorages": [{"name": "default", "gitalyReplicas": 3, "defaultReplicationFactor": 3}],
                "dbSecret": {"secret": PRAEFECT_PASSWORD.name, "key": "password"},
                "psql": {
                    "host": GITLAB_DB.rw.host,
                    "port": GITLAB_DB.rw.port.number,
                    "user": PRAEFECT_DATABASE,
                    "dbName": PRAEFECT_DATABASE,
                    "sslMode": "disable",
                },
            },
            "registry": {"bucket": REGISTRY_BUCKET},
            "kas": {"enabled": False},
            "appConfig": app_config,
            "nodeSelector": dict(node_scheduling.HIL_OVH_NODE_SELECTOR),
        },
        "installCertmanager": False,
        "prometheus": {"install": False},
        "gitlab-runner": {"install": False},
        "registry": {"storage": {"secret": f"{REGISTRY_BUCKET}-connection", "key": "config"}},
        "gitlab": {
            "gitaly": {
                "persistence": {
                    "enabled": True,
                    "storageClass": "piraeus-hdd-local",
                    "accessMode": "ReadWriteOnce",
                    "size": "50Gi",
                },
                "antiAffinity": "hard",
                "affinity": {"podAntiAffinity": {"topologyKey": "kubernetes.io/hostname"}},
                # The chart's cgroup init container needs a writable host cgroup mount. Local
                # Gitaly process isolation stays off until Talos policy and mount support are proven.
                "cgroups": {"enabled": False},
            },
            "praefect": {
                "replicas": 3,
                "antiAffinity": "hard",
                "affinity": {"podAntiAffinity": {"topologyKey": "kubernetes.io/hostname"}},
            },
            "webservice": {"minReplicas": 2, "maxReplicas": 2, "workerProcesses": 1},
            "sidekiq": {"maxReplicas": 2, "concurrency": 10, "livenessProbe": {"initialDelaySeconds": 300}},
            "toolbox": {
                "backups": {
                    "objectStorage": {"backend": "s3", "config": {"secret": BACKUP_S3CFG_SECRET, "key": "config"}}
                },
                # GitLab renders these with `tpl` as YAML text, not a values list.
                "extraVolumes": (
                    "- name: spike-bootstrap\n"
                    f"  configMap:\n    name: {BOOTSTRAP_CONFIG_MAP}\n"
                    "- name: spike-token\n"
                    f"  secret:\n    secretName: {BOOTSTRAP_TOKEN.secret.name}\n"
                ),
                "extraVolumeMounts": (
                    "- name: spike-bootstrap\n  mountPath: /opt/spike-bootstrap\n  readOnly: true\n"
                    "- name: spike-token\n  mountPath: /opt/spike-token\n  readOnly: true\n"
                ),
            },
        },
    }


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://charts.gitlab.io"),
        chart="gitlab",
        version=CHART_VERSION,
        interval="15m",
        timeout="30m",
        install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=-1)),
        upgrade=HelmReleaseSpecUpgrade(remediation=HelmReleaseSpecUpgradeRemediation(retries=3)),
        values=_values(),
    )
    https_route(
        chart,
        "web-route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=WEB,
        listener=None,
        timeout="900s",
    )
    https_route(
        chart,
        "registry-route",
        metadata=ApiObjectMetadata(name=f"{NAME}-registry", namespace=NAMESPACE),
        hostnames=[REGISTRY_HOSTNAME],
        backend=REGISTRY,
        listener=None,
        timeout="900s",
    )
    _ssh_listener(chart)
    return chart
