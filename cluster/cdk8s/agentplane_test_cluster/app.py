"""Candidate chart for Agentplane's persistent, single-node shared test cluster."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Yaml
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import cilium, namespaces
from cluster.cdk8s.agentplane_test_cluster import security
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.oci_cache import zot
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy

NAME = "agentplane-test-cluster"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
IMAGE = "git.allegedly.works/ducktape-ci/agentplane-test-node:unset"
_NODE_NAME = "shared-test-node"
_API_SERVICE = "api"
_API_PORT = 6443
_API_DNS = f"{_API_SERVICE}.{NAME}.svc.cluster.local"
_API_SHORT_DNS = f"{_API_SERVICE}.{NAME}.svc"
_CONFIG_MAP = f"{NAME}-k3s-config"
_POD_LABELS = {"app.kubernetes.io/name": NAME}
_SELECTOR = {"k8s:io.kubernetes.pod.namespace": NAME, **_POD_LABELS}

_K3S_CONFIG = f"""\
node-name: {_NODE_NAME}
data-dir: /var/lib/rancher/k3s
write-kubeconfig: /etc/rancher/k3s/k3s.yaml
write-kubeconfig-mode: "0600"
tls-san:
  - {_API_SHORT_DNS}
  - {_API_DNS}
cluster-cidr: 10.80.0.0/16
service-cidr: 10.81.0.0/16
flannel-backend: host-gw
disable:
  - traefik
  - metrics-server
  - servicelb
disable-network-policy: false
disable-default-registry-endpoint: true
kube-apiserver-arg:
  - feature-gates=ImageVolume=true
kubelet-arg:
  - cgroup-driver=cgroupfs
  - cgroup-root=/workloads
  - feature-gates=ImageVolume=true
"""

# Every upstream enabled in the existing Zot pull-through cache is routed through
# that in-cluster Service. The K3s default endpoint is disabled below, so containerd
# cannot fall back to a public registry when a mirror pull fails.
_REGISTRIES = {
    "mirrors": {
        registry: {"endpoint": [f"http://{zot.HTTP.fqdn}"], "rewrite": {"^(.*)$": f"{prefix}/$1"}}
        for registry, prefix in {
            "docker.io": "docker-hub",
            "ghcr.io": "ghcr",
            "quay.io": "quay",
            "registry.k8s.io": "k8s",
            "gcr.io": "gcr",
        }.items()
    }
}


def _config_map(chart: Chart) -> None:
    # This chart is serialized into K3s's auto-deploy directory, not attached to the outer
    # cdk8s App: these resources belong only to the inner API server.
    inner_app = App(outdir=App.of(chart).outdir)
    inner_security = Yaml.format_objects(security.chart(inner_app).to_json())
    k8s.KubeConfigMap(
        chart,
        "k3s-config",
        metadata=k8s.ObjectMeta(
            name=_CONFIG_MAP,
            namespace=NAME,
            annotations={"description": "K3s server settings and its in-cluster OCI mirrors."},
        ),
        data={
            "config.yaml": _K3S_CONFIG,
            "registries.yaml": yaml_config(_REGISTRIES),
            "00-shared-security.yaml": inner_security,
        },
    )


def _persistent_data(chart: Chart) -> None:
    k8s.KubePersistentVolumeClaim(
        chart,
        "data-pvc",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAME,
            annotations={"description": "Persistent K3s data, kubelet state, logs, and CNI state."},
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="piraeus-ssd-local",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("80Gi")}),
        ),
    )


def _ready_probe(*, period_seconds: int, failure_threshold: int) -> k8s.Probe:
    return k8s.Probe(
        exec=k8s.ExecAction(
            command=["/bin/k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml", "get", "--raw=/readyz"]
        ),
        period_seconds=period_seconds,
        timeout_seconds=5,
        failure_threshold=failure_threshold,
    )


def _deployment(chart: Chart, *, image: str) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAME,
            labels=_POD_LABELS,
            annotations={"description": "The trusted privileged K3s node for Agentplane system tests."},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_POD_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_POD_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    node_selector={"kubernetes.io/hostname": "wyrm2"},
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name="node",
                            image=image,
                            ports=[k8s.ContainerPort(name="https", container_port=_API_PORT, protocol="TCP")],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("4"),
                                    "memory": k8s.Quantity.from_string("12Gi"),
                                    "ephemeral-storage": k8s.Quantity.from_string("2Gi"),
                                    "devices.kubevirt.io/kvm": k8s.Quantity.from_number(1),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("8"),
                                    "memory": k8s.Quantity.from_string("24Gi"),
                                    "ephemeral-storage": k8s.Quantity.from_string("4Gi"),
                                    "devices.kubevirt.io/kvm": k8s.Quantity.from_number(1),
                                },
                            ),
                            security_context=k8s.SecurityContext(
                                privileged=True, allow_privilege_escalation=True, run_as_user=0, run_as_group=0
                            ),
                            lifecycle=k8s.Lifecycle(
                                post_start=k8s.LifecycleHandler(
                                    exec=k8s.ExecAction(command=["/bin/sh", "/opt/agentplane-test-cluster/bootstrap"])
                                )
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="k3s-config",
                                    mount_path="/etc/rancher/k3s/config.yaml",
                                    sub_path="config.yaml",
                                    read_only=True,
                                ),
                                k8s.VolumeMount(
                                    name="k3s-config",
                                    mount_path="/etc/rancher/k3s/registries.yaml",
                                    sub_path="registries.yaml",
                                    read_only=True,
                                ),
                                # ConfigMap subPath updates require a Pod restart; the cluster-wide Reloader
                                # watches ConfigMap changes and restarts this workload by default.
                                k8s.VolumeMount(
                                    name="k3s-config",
                                    mount_path=("/var/lib/rancher/k3s/server/manifests/00-shared-security.yaml"),
                                    sub_path="00-shared-security.yaml",
                                    read_only=True,
                                ),
                                k8s.VolumeMount(name="data", mount_path="/var/lib/rancher/k3s", sub_path="data"),
                                k8s.VolumeMount(name="data", mount_path="/var/lib/kubelet", sub_path="kubelet"),
                                k8s.VolumeMount(name="data", mount_path="/var/log", sub_path="logs"),
                                k8s.VolumeMount(name="data", mount_path="/var/lib/cni", sub_path="cni"),
                            ],
                            startup_probe=_ready_probe(period_seconds=5, failure_threshold=60),
                            readiness_probe=_ready_probe(period_seconds=10, failure_threshold=3),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="k3s-config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP)),
                        k8s.Volume(
                            name="data", persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=NAME)
                        ),
                    ],
                ),
            ),
        ),
    )


def _api_service(chart: Chart) -> None:
    k8s.KubeService(
        chart,
        "api-service",
        metadata=k8s.ObjectMeta(
            name=_API_SERVICE,
            namespace=NAME,
            annotations={"description": "ClusterIP endpoint for the inner Kubernetes API over authenticated TLS."},
        ),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=_POD_LABELS,
            ports=[
                k8s.ServicePort(
                    name="https", port=_API_PORT, target_port=k8s.IntOrString.from_number(_API_PORT), protocol="TCP"
                )
            ],
        ),
    )


def _network_policy(chart: Chart) -> None:
    # Cilium applies egress policy after Service translation, so Zot's Pod target port
    # is 5000 even though its ClusterIP exposes port 80.
    NetworkPolicy(
        chart,
        "node-network-policy",
        metadata=ApiObjectMetadata(
            name=f"{NAME}-network-policy",
            namespace=NAME,
            annotations={"description": "Limits the K3s node to executor API ingress, cluster DNS, and Zot pulls."},
        ),
        endpoint_selector=_SELECTOR,
        ingress=[
            IngressRule.from_endpoints({"k8s:io.kubernetes.pod.namespace": "buildbuddy-executor"}, ports=[_API_PORT])
        ],
        egress=[cilium.dns_egress(), zot.HTTP.egress()],
    )


def chart(app: App, *, image: str) -> Chart:
    """Build the persistent shared cluster with the caller-supplied trusted node image."""
    result = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        result,
        "namespace",
        name=NAME,
        vpa=Vpa.DISABLED,
        labels={
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/audit": "privileged",
            "pod-security.kubernetes.io/warn": "privileged",
        },
    )
    _config_map(result)
    _persistent_data(result)
    forgejo_images_creds_external_secret(result, "forgejo-images-creds", namespace=NAME)
    _deployment(result, image=image)
    _api_service(result)
    _network_policy(result)
    return result


def agentplane_test_cluster(
    chart: Chart,
    directory: RenderedDirectory,
    *,
    oci_cache: Kustomization,
    piraeus: Kustomization,
    external_secrets_operator: Kustomization,
    forgejo_images: Kustomization,
    forgejo_image_automation: Kustomization,
    kubevirt: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    """Own the persistent PVC without activating the privileged test cluster yet."""
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="10m",
        suspend=True,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(
            oci_cache, piraeus, external_secrets_operator, forgejo_images, forgejo_image_automation, kubevirt, kyverno
        ),
        description=(
            "Persistent inner K3s test cluster on wyrm2. Suspended until the trusted-node safety bootstrap, "
            "runtime proof, and first published image are ready."
        ),
    )
