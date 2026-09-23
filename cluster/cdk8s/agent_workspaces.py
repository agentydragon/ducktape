"""agent-workspaces: disposable agent workspaces from the agent-sandbox controller -- the
namespace, its quota and limits, the codex-lane SandboxTemplate and the janitor. Usage:
cluster/k8s/agents/agent-sandbox/README.md.

Hand-written beside the output: `sandboxwarmpool-codex.yaml` (no SandboxWarmPool binding yet)
and `image-pins/kustomization.yaml`, which overrides the workspace image's `unset` tag.
"""

from __future__ import annotations

from pathlib import Path

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplate,
    SandboxTemplateSpec,
    SandboxTemplateSpecNetworkPolicyManagement,
    SandboxTemplateSpecPodTemplate,
    SandboxTemplateSpecPodTemplateMetadata,
    SandboxTemplateSpecPodTemplateSpec,
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersEnvValueFrom,
    SandboxTemplateSpecPodTemplateSpecContainersEnvValueFromSecretKeyRef,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContext,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
    SandboxTemplateSpecPodTemplateSpecImagePullSecrets,
    SandboxTemplateSpecPodTemplateSpecSecurityContext,
    SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile,
    SandboxTemplateSpecVolumeClaimTemplates,
    SandboxTemplateSpecVolumeClaimTemplatesMetadata,
    SandboxTemplateSpecVolumeClaimTemplatesPolicy,
    SandboxTemplateSpecVolumeClaimTemplatesSpec,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResources,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests,
)
from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from kyverno_cleanuppolicy_crds.io.kyverno import (
    CleanupPolicy,
    CleanupPolicySpec,
    CleanupPolicySpecConditions,
    CleanupPolicySpecConditionsAll,
    CleanupPolicySpecConditionsAllOperator,
    CleanupPolicySpecMatch,
    CleanupPolicySpecMatchAny,
    CleanupPolicySpecMatchAnyResources,
)

from cluster.cdk8s import forgejo_images
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "agent-workspaces"
NAMESPACE = "agent-workspaces"
OUTPUT_DIR = "cluster/k8s/agents/agent-sandbox/workspaces"


def _quantities(values: dict[str, str]) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _codex_template(chart: Chart) -> None:
    """codex LLM lane: OpenAI Codex-account models via the cluster LiteLLM. The
    networkPolicyManagement/dnsPolicy/storageClassName settings are load-bearing. The codex CLI
    picks up the baked ~/.codex/config.toml (workspace-image/codex-config.toml), whose provider
    reads the key from LITELLM_API_KEY."""
    SandboxTemplate(
        chart,
        "codex",
        metadata=metadata("codex", NAMESPACE),
        spec=SandboxTemplateSpec(
            network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
            pod_template=SandboxTemplateSpecPodTemplate(
                metadata=SandboxTemplateSpecPodTemplateMetadata(labels={"app.kubernetes.io/name": "agent-workspace"}),
                spec=SandboxTemplateSpecPodTemplateSpec(
                    dns_policy="ClusterFirst",
                    image_pull_secrets=[
                        SandboxTemplateSpecPodTemplateSpecImagePullSecrets(name=forgejo_images.SECRET_NAME)
                    ],
                    # No explicit region nodeSelector: the workspace volumeClaimTemplate's
                    # seaweedfs-ovh StorageClass already pins scheduling to
                    # topology.kubernetes.io/zone=hil-ovh via WaitForFirstConsumer +
                    # allowedTopologies (cluster/k8s/seaweedfs-csi/sc-seaweedfs-ovh.yaml).
                    automount_service_account_token=False,
                    security_context=SandboxTemplateSpecPodTemplateSpecSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                        seccomp_profile=SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile(
                            type="RuntimeDefault"
                        ),
                    ),
                    containers=[
                        SandboxTemplateSpecPodTemplateSpecContainers(
                            name="workspace",
                            # image-pins/ sets the tag.
                            image="git.allegedly.works/ducktape-ci/agent-workspace:unset",
                            command=["sleep", "infinity"],
                            working_dir="/workspace",
                            security_context=SandboxTemplateSpecPodTemplateSpecContainersSecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities(
                                    drop=["ALL"]
                                ),
                            ),
                            env=[
                                # LiteLLM virtual key (alias agent-workspaces-codex,
                                # `chatgpt/oai-responses/*` models, no budget cap) minted by
                                # tf/gitops/litellm-keys and reflected into this namespace; deleting
                                # that TF resource is the lane's kill switch.
                                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                                    name="LITELLM_API_KEY",
                                    value_from=SandboxTemplateSpecPodTemplateSpecContainersEnvValueFrom(
                                        secret_key_ref=SandboxTemplateSpecPodTemplateSpecContainersEnvValueFromSecretKeyRef(
                                            name="litellm-key-agent-workspaces-codex", key="api-key"
                                        )
                                    ),
                                )
                            ],
                            resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
                                requests={
                                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string(
                                        "500m"
                                    ),
                                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string(
                                        "1Gi"
                                    ),
                                },
                                limits={
                                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("2"),
                                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string(
                                        "4Gi"
                                    ),
                                },
                            ),
                            volume_mounts=[
                                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                                    name="workspace", mount_path="/workspace"
                                )
                            ],
                        )
                    ],
                ),
            ),
            volume_claim_templates_policy=SandboxTemplateSpecVolumeClaimTemplatesPolicy.OVERRIDES,
            volume_claim_templates=[
                SandboxTemplateSpecVolumeClaimTemplates(
                    metadata=SandboxTemplateSpecVolumeClaimTemplatesMetadata(name="workspace"),
                    spec=SandboxTemplateSpecVolumeClaimTemplatesSpec(
                        storage_class_name="seaweedfs-ovh",
                        access_modes=["ReadWriteOnce"],
                        resources=SandboxTemplateSpecVolumeClaimTemplatesSpecResources(
                            requests={
                                "storage": SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests.from_string(
                                    "10Gi"
                                )
                            }
                        ),
                    ),
                )
            ],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE, labels={"name": NAMESPACE}))
    # Sized for ~a dozen concurrent workspaces (each requests 500m/1Gi + a 10Gi PVC per the
    # workspace SandboxTemplate) plus warm-pool idle capacity.
    k8s.KubeResourceQuota(
        chart,
        "quota",
        metadata=k8s.ObjectMeta(name="agent-workspaces-quota", namespace=NAMESPACE),
        spec=k8s.ResourceQuotaSpec(
            hard=_quantities(
                {
                    "requests.cpu": "8",
                    "requests.memory": "16Gi",
                    "limits.cpu": "16",
                    "limits.memory": "32Gi",
                    "pods": "12",
                    "services": "12",
                    "configmaps": "20",
                    "persistentvolumeclaims": "15",
                    "requests.storage": "150Gi",
                }
            )
        ),
    )
    k8s.KubeLimitRange(
        chart,
        "limits",
        metadata=k8s.ObjectMeta(name="agent-workspaces-limits", namespace=NAMESPACE),
        spec=k8s.LimitRangeSpec(
            limits=[
                k8s.LimitRangeItem(
                    type="Container",
                    max=_quantities({"cpu": "4", "memory": "8Gi"}),
                    min=_quantities({"cpu": "10m", "memory": "16Mi"}),
                    default=_quantities({"cpu": "500m", "memory": "512Mi"}),
                    default_request=_quantities({"cpu": "100m", "memory": "128Mi"}),
                ),
                k8s.LimitRangeItem(type="Pod", max=_quantities({"cpu": "8", "memory": "16Gi"})),
            ]
        ),
    )
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    _codex_template(chart)
    # Workspaces are ephemeral by contract (same 7-day rule as claude-sandbox's sandbox-janitor):
    # a Sandbox whose owner forgot shutdownTime (default shutdownPolicy is Retain) would otherwise
    # pin quota forever. Reaping happens at the CR level, not the pod level -- the controller
    # recreates a Sandbox's pod, so a pod-level janitor would just cause churn, not cleanup.
    # Warm-pool sandboxes reaped at 7d are recreated by their pool; that periodic refresh is
    # harmless. Delete RBAC: kyverno/policies/clusterrole-cleanup-controller-sandboxes.yaml.
    CleanupPolicy(
        chart,
        "janitor",
        metadata=metadata("workspace-janitor", NAMESPACE),
        spec=CleanupPolicySpec(
            schedule="40 * * * *",
            match=CleanupPolicySpecMatch(
                any=[
                    CleanupPolicySpecMatchAny(
                        resources=CleanupPolicySpecMatchAnyResources(
                            kinds=["agents.x-k8s.io/v1beta1/Sandbox", "extensions.agents.x-k8s.io/v1beta1/SandboxClaim"]
                        )
                    )
                ]
            ),
            conditions=CleanupPolicySpecConditions(
                all=[
                    CleanupPolicySpecConditionsAll(
                        key="{{ time_since('', '{{ target.metadata.creationTimestamp }}', '') }}",
                        operator=CleanupPolicySpecConditionsAllOperator.GREATER_THAN,
                        value="168h",
                    )
                ]
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
