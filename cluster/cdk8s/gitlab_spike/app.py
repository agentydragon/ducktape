"""GitLab spike chart: three independent Gitaly disks behind Praefect."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeRemediation,
)

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.cilium import tcp_listener
from cluster.cdk8s.cnpg import cluster
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import VPA_UPDATE_MODE_LABEL, Vpa
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.vpa_policy import (
    VPA_RESOURCE_POLICY_ANNOTATION,
    ContainerPolicy,
    ControlledValues,
    resource_policy_annotation,
)

NAME = "gitlab-spike"
NAMESPACE = NAME
OUTPUT_DIR = f"{GENERATED_ROOT}/gitlab-spike/app"
HOSTNAME = "gitlab-spike.allegedly.works"
REGISTRY_HOSTNAME = "registry-gitlab-spike.allegedly.works"
CHART_VERSION = "10.5.1"  # GitLab 19.5.1, published 2026-10-21 in charts.gitlab.io.
_SHELL_CPU_REQUEST = "23m"  # Current admitted request on both GitLab Shell replicas.

GITLAB_DB = cluster.PostgresRef.generated(name="gitlab-db", namespace=NAMESPACE)
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


def _vpa_annotations(*policies: ContainerPolicy, mode: Vpa = Vpa.INITIAL) -> dict[str, str]:
    return {
        VPA_UPDATE_MODE_LABEL: mode,
        VPA_RESOURCE_POLICY_ANNOTATION: resource_policy_annotation(
            *policies, ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"), compact=True
        ),
    }


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
        "registry": {
            "storage": {"secret": f"{REGISTRY_BUCKET}-connection", "key": "config"},
            # Spread each ReplicaSet across eligible hosts while allowing the HPA to scale beyond the host count.
            "topologySpreadConstraints": [
                {
                    "maxSkew": 1,
                    "topologyKey": "kubernetes.io/hostname",
                    "whenUnsatisfiable": "DoNotSchedule",
                    "nodeTaintsPolicy": "Honor",
                    "labelSelector": {"matchLabels": REGISTRY.pods.selector},
                    "matchLabelKeys": ["pod-template-hash"],
                }
            ],
            "deployment": {
                "annotations": _vpa_annotations(
                    # Registry's HPA uses CPU utilization, which depends on the request.
                    ContainerPolicy.memory_requests_only(
                        "registry", cpu_request="50m", memory_floor="96Mi", memory_limit="128Mi"
                    ),
                    mode=Vpa.AUTO,
                )
            },
        },
        "gitlab": {
            "gitlab-exporter": {
                "deployment": {
                    "annotations": _vpa_annotations(
                        ContainerPolicy.memory_requests_only(
                            "gitlab-exporter", cpu_request="75m", memory_floor="96Mi", memory_limit="128Mi"
                        )
                    )
                }
            },
            "gitlab-shell": {
                # Keep the current CPU share in the template while the HPA owns CPU.
                # The init containers already reserve 50m, so the Pod request stays at 50m.
                "resources": {"requests": {"cpu": _SHELL_CPU_REQUEST}},
                "deployment": {
                    "annotations": _vpa_annotations(
                        ContainerPolicy.memory_requests_only(
                            "gitlab-shell", cpu_request=_SHELL_CPU_REQUEST, memory_floor="96Mi", memory_limit="128Mi"
                        ),
                        mode=Vpa.AUTO,
                    )
                },
            },
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
            "webservice": {
                "minReplicas": 2,
                "maxReplicas": 2,
                "workerProcesses": 1,
                "deployment": {
                    "startupProbe": {
                        "httpGet": {"path": "/-/liveness", "port": 8080},
                        "periodSeconds": 5,
                        "timeoutSeconds": 5,
                        "failureThreshold": 60,
                    }
                },
            },
            "sidekiq": {
                "maxReplicas": 2,
                "concurrency": 10,
                "livenessProbe": {"initialDelaySeconds": 300},
                "deployment": {
                    "annotations": _vpa_annotations(
                        ContainerPolicy.memory_requests_only(
                            "sidekiq", cpu_request="900m", memory_floor="3Gi", memory_limit="4Gi"
                        )
                    )
                },
            },
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
    tcp_listener(chart, "ssh-listener", service=SSH)
    return chart
