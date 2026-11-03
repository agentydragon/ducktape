"""The static, VM-isolated BuildBuddy worker for opt-in KVM tests."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainCpuFeatures,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesLimits,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesEmptyDisk,
    VirtualMachineSpecTemplateSpecVolumesEmptyDiskCapacity,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry import pull_credentials
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_vm, domain_labels
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.oci_cache import zot
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy, isolate_ingress

NAME = "buildbuddy-kvm-worker"
NAMESPACE = "buildbuddy-executor"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
_IMAGE = "git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker:unset"
_HTTPS_HOSTS = ("remote.buildbuddy.io", "buildbuddy.bbcr.io")


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    pull_credentials.forgejo_images_creds_external_secret(chart, "pull-credentials", namespace=NAMESPACE)
    k8s.KubeServiceAccount(
        chart,
        "default-service-account",
        metadata=k8s.ObjectMeta(name="default", namespace=NAMESPACE),
        automount_service_account_token=False,
        image_pull_secrets=[k8s.LocalObjectReference(name=pull_credentials.SECRET_NAME)],
    )
    container_disk_vm(
        chart,
        "worker",
        name=NAME,
        namespace=NAMESPACE,
        image=_IMAGE,
        cpu=VirtualMachineSpecTemplateSpecDomainCpu(
            cores=8,
            model="host-passthrough",
            features=[VirtualMachineSpecTemplateSpecDomainCpuFeatures(name="svm", policy="require")],
        ),
        resources=VirtualMachineSpecTemplateSpecDomainResources(
            requests={
                "cpu": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("8"),
                "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("24Gi"),
            },
            limits={
                "cpu": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string("8"),
                "memory": VirtualMachineSpecTemplateSpecDomainResourcesLimits.from_string("24Gi"),
            },
        ),
        node_selector={"kubernetes.io/hostname": "wyrm2"},
        ports=[],
        disks={
            "work": VirtualMachineSpecTemplateSpecVolumes(
                name="work",
                empty_disk=VirtualMachineSpecTemplateSpecVolumesEmptyDisk(
                    capacity=VirtualMachineSpecTemplateSpecVolumesEmptyDiskCapacity.from_string("80Gi")
                ),
            ),
            "bbkey": VirtualMachineSpecTemplateSpecVolumes(
                name="buildbuddy-key",
                secret=VirtualMachineSpecTemplateSpecVolumesSecret(secret_name="buildbuddy-api-key"),
            ),
        },
        autoattach_graphics_device=False,
        annotations={
            "description": (
                "Static trusted-code BuildBuddy worker. Privileged Docker test actions run inside the guest; "
                "the guest has no outer Kubernetes token or host filesystem mounts."
            )
        },
    )
    NetworkPolicy(
        chart,
        "network",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        endpoint_selector=domain_labels(NAME),
        ingress=isolate_ingress(),
        egress=[
            cilium.dns_egress(resolves=["*"]),
            EgressRule.to_fqdns(*_HTTPS_HOSTS),
            EgressRule.to_endpoints(cilium.endpoint_labels(zot.HTTP.pods.namespace, "zot"), zot.HTTP.pod_port),
        ],
    )
    return chart


def buildbuddy_kvm_worker(
    chart: Chart,
    directory: RenderedDirectory,
    executor: Kustomization,
    kubevirt: Kustomization,
    external_secrets: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        # Select the first published worker image before resuming through Git.
        suspend=True,
        depends_on=flux_kustomization_depends_on_many(executor, kubevirt, external_secrets),
    )
