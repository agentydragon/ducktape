"""cpap-sync: the daily CronJob that copies the CPAP card's EDF files into the cpap-data
Forgejo repo, the repo's git credentials copied from the forgejo namespace, the KubeVirt
gateway VM that exposes the card, the Service fronting the card's HTTP API, and the Job's
egress policy.

Hand-written beside the generated output: the card's SOPS Secret, and `image-pins/`, whose
image-automation markers override this chart's placeholder image tags
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumNetworkPolicySpecEgressToServices,
    CiliumNetworkPolicySpecEgressToServicesK8SService,
)
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevicesHostDevices,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)

from cluster.cdk8s import cilium, namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.forgejo_registry import chart as forgejo_images
from cluster.cdk8s.kubevirt.virtual_machine import container_disk_vm, domain_labels
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.kubevirt.virtual_machine import VirtualMachine
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.vm_image_restart import OPT_IN_ANNOTATION, VmImageRestartController

NAME = "cpap-sync"
NAMESPACE = "cpap-sync"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/cpap-sync"
# The tags come from image-pins/kustomization.yaml.
_IMAGE = "git.allegedly.works/ducktape-ci/cpap-sync:unset"
_GATEWAY_IMAGE = "git.allegedly.works/ducktape-ci/cpap-gateway:unset"
_LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/component": "sync"}
_GATEWAY = "cpap-gateway"
# The gateway VM's forwarder listens on 18080; the Service presents it on 80.
_CARD = ServiceRef(
    name="cpap-card",
    port=Port(name="http", number=80),
    pods=Pods(namespace=NAMESPACE, labels=tuple(domain_labels(_GATEWAY).items())),
    target_port=18080,
)
_GIT_CREDENTIALS = SecretRef(namespace=NAMESPACE, name="cpap-data-git-write")
_GIT_READ_CREDENTIALS = "cpap-data-git-read"
_WORKDIR = "/workdir"
_CARD_SECRET = "cpap-ezshare"
CARD_SECRET_FILE = f"{_CARD_SECRET}.sops.yaml"


def _gateway_vm(chart: Chart) -> VirtualMachine:
    return container_disk_vm(
        chart,
        "gateway-vm",
        name=_GATEWAY,
        namespace=NAMESPACE,
        annotations={
            OPT_IN_ANNOTATION: "true",
            "description": (
                "Always-on KubeVirt gateway for the CPAP ez Share WiFi card. The USB adapter stays physically "
                "attached to OptiPlex and is passed through to this VM; the VM exposes only the card's HTTP API "
                "to the sync Service."
            ),
        },
        # A stateless VM disk.
        image=_GATEWAY_IMAGE,
        cpu=VirtualMachineSpecTemplateSpecDomainCpu(cores=2),
        resources=VirtualMachineSpecTemplateSpecDomainResources(
            requests={
                # The guest currently has substantial headroom at this size. Keep
                # the appliance small while KubeVirt over-reserves USB host devices
                # as VFIO memory overhead.
                "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("768Mi")
            }
        ),
        # The host the CPAP card's USB WiFi adapter is attached to.
        node_selector=node_scheduling.OPTIPLEX.node_selector,
        ports=[VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(name="http", port=_CARD.pod_port)],
        disks={
            "cpapsecret": VirtualMachineSpecTemplateSpecVolumes(
                name="cpap-secret", secret=VirtualMachineSpecTemplateSpecVolumesSecret(secret_name=_CARD_SECRET)
            )
        },
        host_devices=[
            VirtualMachineSpecTemplateSpecDomainDevicesHostDevices(
                name="cpap-wifi", device_name="kubevirt.io/cpap-wifi"
            )
        ],
        # This appliance has no graphical console.
        autoattach_graphics_device=False,
    )


def namespace_chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.DISABLED,
        labels={"name": NAMESPACE, "pod-security.kubernetes.io/enforce": "baseline"},
    )
    return chart


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    # tf/gitops/cpap-data's service-user credentials: the writer's for the CronJob, and the
    # reader's for analysis, mirrored into claude-sandbox for Claude Code sessions.
    reader = secret_copy.reader(chart, NAMESPACE)
    secret_copy.secret_copy(chart, _GIT_CREDENTIALS.name, reader=reader)
    secret_copy.secret_copy(chart, _GIT_READ_CREDENTIALS, reader=reader, mirror_namespaces=["claude-sandbox"])
    k8s.KubeServiceAccount(
        chart,
        "default-service-account",
        metadata=k8s.ObjectMeta(name="default", namespace=NAMESPACE),
        image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
    )
    k8s.KubeCronJob(
        chart,
        "cronjob",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Connects to the ez Share WiFi SD card through the cpap-card gateway Service and syncs all EDF "
                    "data files into the cpap-data Forgejo repo (partial clone + commit + push; credentials from "
                    "the cpap-data-git-write secret)."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="0 10 * * *",
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    backoff_limit=2,
                    template=k8s.PodTemplateSpec(
                        metadata=k8s.ObjectMeta(labels=_LABELS),
                        spec=k8s.PodSpec(
                            restart_policy="OnFailure",
                            node_selector=node_scheduling.OPTIPLEX.node_selector,
                            automount_service_account_token=False,
                            volumes=[k8s.Volume(name="workdir", empty_dir=k8s.EmptyDirVolumeSource())],
                            containers=[
                                k8s.Container(
                                    name="sync",
                                    image=_IMAGE,
                                    # The aspect py_binary launcher materializes its venv under the
                                    # image runfiles directory at startup, so this container needs
                                    # a writable root filesystem despite the other hardening here.
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False,
                                        capabilities=k8s.Capabilities(drop=["ALL"]),
                                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                                    ),
                                    args=[
                                        "--base-url",
                                        _CARD.url,
                                        "--git-url",
                                        "$(GIT_REPO_URL)",
                                        "--wifi-interface",
                                        "",
                                    ],
                                    env=[
                                        # The clone (initial re-seed: the card's full history) lands in
                                        # TMPDIR — keep it on the emptyDir, not the container layer.
                                        k8s.EnvVar(name="TMPDIR", value=_WORKDIR),
                                        _GIT_CREDENTIALS.key("repo_url").env_var("GIT_REPO_URL"),
                                        _GIT_CREDENTIALS.key("username").env_var("GIT_USERNAME"),
                                        _GIT_CREDENTIALS.key("password").env_var("GIT_PASSWORD"),
                                    ],
                                    volume_mounts=[k8s.VolumeMount(name="workdir", mount_path=_WORKDIR)],
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("100m"),
                                            "memory": k8s.Quantity.from_string("128Mi"),
                                        },
                                        # git pack-objects on the initial ~0.5-1GB seed push.
                                        limits={"memory": k8s.Quantity.from_string("512Mi")},
                                    ),
                                )
                            ],
                        ),
                    ),
                )
            ),
        ),
    )

    k8s.KubeService(
        chart,
        "card-service",
        metadata=k8s.ObjectMeta(
            name=_CARD.name,
            namespace=_CARD.pods.namespace,
            annotations={
                "description": (
                    "Internal ClusterIP facade for the CPAP card HTTP API exposed by the KubeVirt gateway VM on "
                    "the OptiPlex."
                )
            },
        ),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=_CARD.pods.selector,
            ports=[
                k8s.ServicePort(
                    name=_CARD.port.name,
                    port=_CARD.port.number,
                    protocol="TCP",
                    target_port=k8s.IntOrString.from_number(_CARD.pod_port),
                )
            ],
        ),
    )
    gateway = _gateway_vm(chart)
    VmImageRestartController(chart, "vm-image-restart-controller", target_vm=gateway)
    NetworkPolicy(
        chart,
        "egress",
        metadata=ApiObjectMetadata(
            name="egress",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "The sync Job may resolve DNS, reach the in-cluster CPAP gateway Service, and push to Forgejo "
                    "through the cluster gateway."
                )
            },
        ),
        endpoint_selector={"app.kubernetes.io/name": NAME},
        egress=[
            cilium.dns_egress(),
            # The Service is port 80, but Cilium enforces the translated backend
            # targetPort after socket-level load balancing (18080). See
            # cluster/docs/cilium_network_policy.md.
            CiliumNetworkPolicySpecEgress(
                to_services=[
                    CiliumNetworkPolicySpecEgressToServices(
                        k8_s_service=CiliumNetworkPolicySpecEgressToServicesK8SService(
                            service_name=_CARD.name, namespace=_CARD.pods.namespace
                        )
                    )
                ],
                to_ports=[
                    CiliumNetworkPolicySpecEgressToPorts(
                        ports=[
                            CiliumNetworkPolicySpecEgressToPortsPorts(
                                port=str(_CARD.pod_port), protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                            )
                        ]
                    )
                ],
            ),
            # cpap-card is backed by a KubeVirt virt-launcher rather than an ordinary
            # Deployment pod.  On this cluster's Cilium/KubeVirt path, toServices
            # compiles to the right selector but does not install a usable BPF allow
            # for the translated backend connection.  Keep the Service rule above for
            # the facade contract and explicitly authorize the stable VMI identity.
            _CARD.egress(),
            # git.allegedly.works resolves to the cluster's Gateway node addresses;
            # cluster covers those node entities as well as in-cluster Forgejo traffic.
            EgressRule.to_entities(Entity.CLUSTER, ports=[443, 3000]),
        ],
    )
    return chart


def cpap_sync(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization, kubevirt: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="30m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, kubevirt),
    )
