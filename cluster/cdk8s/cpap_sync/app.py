"""cpap-sync: the daily CronJob that copies the CPAP card's EDF files into the cpap-data
Forgejo repo, the KubeVirt gateway VM that exposes the card, the Service fronting the card's
HTTP API, and the Job's egress policy.

Hand-written beside the generated output: the card's SOPS Secret, and `image-pins/`, whose
image-automation markers override this chart's placeholder image tags
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
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
    VirtualMachine,
    VirtualMachineSpec,
    VirtualMachineSpecTemplate,
    VirtualMachineSpecTemplateSpec,
    VirtualMachineSpecTemplateSpecDomain,
    VirtualMachineSpecTemplateSpecDomainCpu,
    VirtualMachineSpecTemplateSpecDomainDevices,
    VirtualMachineSpecTemplateSpecDomainDevicesDisks,
    VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk,
    VirtualMachineSpecTemplateSpecDomainDevicesHostDevices,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfaces,
    VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts,
    VirtualMachineSpecTemplateSpecDomainFirmware,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloader,
    VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi,
    VirtualMachineSpecTemplateSpecDomainResources,
    VirtualMachineSpecTemplateSpecDomainResourcesRequests,
    VirtualMachineSpecTemplateSpecNetworks,
    VirtualMachineSpecTemplateSpecNetworksPod,
    VirtualMachineSpecTemplateSpecVolumes,
    VirtualMachineSpecTemplateSpecVolumesContainerDisk,
    VirtualMachineSpecTemplateSpecVolumesSecret,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cilium, forgejo_images
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_namespace, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy

NAME = "cpap-sync"
NAMESPACE = "cpap-sync"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/cpap-sync"
# The tags come from image-pins/kustomization.yaml.
_IMAGE = "git.allegedly.works/ducktape-ci/cpap-sync:unset"
_GATEWAY_IMAGE = "git.allegedly.works/ducktape-ci/cpap-gateway:unset"
_LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/component": "sync"}
_NODE_SELECTOR = {"kubernetes.io/hostname": "optiplex"}  # the host the CPAP card's USB WiFi adapter is attached to
_CARD_SERVICE = "cpap-card"
_GATEWAY = "cpap-gateway"
_GATEWAY_PORT = 18080
_GIT_CREDENTIALS = "cpap-data-git-write"
_WORKDIR = "/workdir"
_CARD_SECRET = "cpap-ezshare"
_RESOURCES = ["namespace.k8s.yaml", f"{_CARD_SECRET}.sops.yaml", f"{NAME}.k8s.yaml"]


def _git_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=_GIT_CREDENTIALS, key=key))
    )


def _gateway_vm(chart: Chart) -> None:
    labels = {"app.kubernetes.io/name": _GATEWAY}
    VirtualMachine(
        chart,
        "gateway-vm",
        metadata=metadata(
            _GATEWAY,
            NAMESPACE,
            labels=labels,
            annotations={
                "description": (
                    "Always-on KubeVirt gateway for the CPAP ez Share WiFi card. The USB adapter stays physically "
                    "attached to OptiPlex and is passed through to this VM; the VM exposes only the card's HTTP API "
                    "to the sync Service."
                )
            },
        ),
        spec=VirtualMachineSpec(
            run_strategy="Always",
            template=VirtualMachineSpecTemplate(
                metadata=k8s.ObjectMeta(labels={"kubevirt.io/domain": _GATEWAY} | labels),
                spec=VirtualMachineSpecTemplateSpec(
                    node_selector=_NODE_SELECTOR,
                    domain=VirtualMachineSpecTemplateSpecDomain(
                        cpu=VirtualMachineSpecTemplateSpecDomainCpu(cores=2),
                        resources=VirtualMachineSpecTemplateSpecDomainResources(
                            requests={
                                # The guest currently has substantial headroom at this size. Keep
                                # the appliance small while KubeVirt over-reserves USB host devices
                                # as VFIO memory overhead.
                                "memory": VirtualMachineSpecTemplateSpecDomainResourcesRequests.from_string("768Mi")
                            }
                        ),
                        firmware=VirtualMachineSpecTemplateSpecDomainFirmware(
                            bootloader=VirtualMachineSpecTemplateSpecDomainFirmwareBootloader(
                                efi=VirtualMachineSpecTemplateSpecDomainFirmwareBootloaderEfi(secure_boot=False)
                            )
                        ),
                        devices=VirtualMachineSpecTemplateSpecDomainDevices(
                            # This appliance has no graphical console.
                            autoattach_graphics_device=False,
                            disks=[
                                VirtualMachineSpecTemplateSpecDomainDevicesDisks(
                                    name="rootdisk",
                                    boot_order=1,
                                    disk=VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio"),
                                ),
                                VirtualMachineSpecTemplateSpecDomainDevicesDisks(
                                    name="cpap-secret",
                                    serial="cpapsecret",
                                    disk=VirtualMachineSpecTemplateSpecDomainDevicesDisksDisk(bus="virtio"),
                                ),
                            ],
                            interfaces=[
                                VirtualMachineSpecTemplateSpecDomainDevicesInterfaces(
                                    name="default",
                                    masquerade={},
                                    ports=[
                                        VirtualMachineSpecTemplateSpecDomainDevicesInterfacesPorts(
                                            name="http", port=_GATEWAY_PORT
                                        )
                                    ],
                                )
                            ],
                            host_devices=[
                                VirtualMachineSpecTemplateSpecDomainDevicesHostDevices(
                                    name="cpap-wifi", device_name="kubevirt.io/cpap-wifi"
                                )
                            ],
                        ),
                    ),
                    networks=[
                        VirtualMachineSpecTemplateSpecNetworks(
                            name="default", pod=VirtualMachineSpecTemplateSpecNetworksPod()
                        )
                    ],
                    volumes=[
                        # A stateless VM disk.
                        VirtualMachineSpecTemplateSpecVolumes(
                            name="rootdisk",
                            container_disk=VirtualMachineSpecTemplateSpecVolumesContainerDisk(
                                image=_GATEWAY_IMAGE, image_pull_policy="IfNotPresent"
                            ),
                        ),
                        VirtualMachineSpecTemplateSpecVolumes(
                            name="cpap-secret",
                            secret=VirtualMachineSpecTemplateSpecVolumesSecret(secret_name=_CARD_SECRET),
                        ),
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
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
                            node_selector=_NODE_SELECTOR,
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
                                        f"http://{_CARD_SERVICE}.{NAMESPACE}.svc.cluster.local",
                                        "--git-url",
                                        "$(GIT_REPO_URL)",
                                        "--wifi-interface",
                                        "",
                                    ],
                                    env=[
                                        # The clone (initial re-seed: the card's full history) lands in
                                        # TMPDIR — keep it on the emptyDir, not the container layer.
                                        k8s.EnvVar(name="TMPDIR", value=_WORKDIR),
                                        _git_env("GIT_REPO_URL", "repo_url"),
                                        _git_env("GIT_USERNAME", "username"),
                                        _git_env("GIT_PASSWORD", "password"),
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
            name=_CARD_SERVICE,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Internal ClusterIP facade for the CPAP card HTTP API exposed by the KubeVirt gateway VM on "
                    "the OptiPlex."
                )
            },
        ),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector={"kubevirt.io/domain": _GATEWAY},
            ports=[
                k8s.ServicePort(
                    name="http", port=80, protocol="TCP", target_port=k8s.IntOrString.from_number(_GATEWAY_PORT)
                )
            ],
        ),
    )
    _gateway_vm(chart)
    NetworkPolicy(
        chart,
        "egress",
        metadata=metadata(
            "cpap-sync-egress",
            NAMESPACE,
            annotations={
                "description": (
                    "The sync Job may resolve DNS, reach the in-cluster CPAP gateway Service, and push to Forgejo "
                    "through the cluster gateway."
                )
            },
        ),
        selector={"app.kubernetes.io/name": NAME},
        egress=[
            cilium.dns_egress(),
            # The Service is port 80, but Cilium enforces the translated backend
            # targetPort after socket-level load balancing (18080). See
            # cluster/docs/cilium_network_policy.md.
            CiliumNetworkPolicySpecEgress(
                to_services=[
                    CiliumNetworkPolicySpecEgressToServices(
                        k8_s_service=CiliumNetworkPolicySpecEgressToServicesK8SService(
                            service_name=_CARD_SERVICE, namespace=NAMESPACE
                        )
                    )
                ],
                to_ports=[
                    CiliumNetworkPolicySpecEgressToPorts(
                        ports=[
                            CiliumNetworkPolicySpecEgressToPortsPorts(
                                port=str(_GATEWAY_PORT), protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
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
            EgressRule.to_endpoints(
                {"k8s:io.kubernetes.pod.namespace": NAMESPACE, "k8s:kubevirt.io/domain": _GATEWAY}, _GATEWAY_PORT
            ),
            # git.allegedly.works resolves to the cluster's Gateway node addresses;
            # cluster covers those node entities as well as in-cluster Forgejo traffic.
            EgressRule.to_entities("cluster", ports=[443, 3000]),
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        OUTPUT_DIR,
        name=NAMESPACE,
        labels={
            "name": NAMESPACE,
            "pod-security.kubernetes.io/enforce": "baseline",
            "goldilocks.fairwinds.com/enabled": "false",
        },
    )
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=_RESOURCES, components=["./image-pins"]),
    )


def cpap_sync(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    kubevirt: Kustomization,
    forgejo_images_kustomization: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        decryption=SOPS_DECRYPTION,
        timeout="30m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_config, kubevirt, forgejo_images_kustomization),
    )
