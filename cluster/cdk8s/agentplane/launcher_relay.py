"""The egress relay of a VM Sandbox. KubeVirt builds a VM's virt-launcher Pod from the VirtualMachine,
not from our SandboxTemplate's Pod, so this Kyverno policy adds the relay when the Pod is created: the
same egress sidecar, routes and projected tokens as a runner Pod's (runner_sandbox.py), copied from the
rendered runner template, with settings rendered from the template's own so the two cannot drift.

The guest reaches the relay at its masquerade gateway, `10.0.2.1`, on the ports a runner Pod's
workload dials on loopback: the HTTP proxy and one listener per platform route (platform_routes.py).
The tokens are mounted into the relay and nowhere else in the Pod, and the guest gets none. The
VirtualMachine's template carries the runner Pods' labels, which KubeVirt copies onto the launcher, so
the runner fence and the platform-route rules apply to it unchanged.

The relay runs as the Sandbox's own ServiceAccount, so the egress proxy grants it what that account's
PolicyBinding grants. The policy admits a launcher only when KubeVirt's controller creates it for a live
VMI of a live VirtualMachine controlled by one of our Sandboxes, and that same Sandbox controls the
ServiceAccount.

PVC-, Secret- and ConfigMap-backed virtiofs daemons run as native sidecars, after KubeVirt's existing
initializers. They stay alive until compute exits, allowing the guest to close journals and unmount
state and credential volumes during shutdown. ContainerPath daemons remain ordinary containers
because KubeVirt reconciles them there.

Failure scope: the policy registers its own webhook, narrowed by match conditions to this namespace's
launcher creates and updates of Pods carrying the token volume. With Kyverno down, only VM Sandboxes
fail to start; nothing else in the namespace or the cluster waits on it.
"""

from __future__ import annotations

from typing import Any

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicySpecFailurePolicy,
    ClusterPolicySpecRules,
    ClusterPolicySpecRulesContext,
    ClusterPolicySpecRulesContextApiCall,
    ClusterPolicySpecRulesContextVariable,
    ClusterPolicySpecRulesMatchAnyResources,
    ClusterPolicySpecRulesMatchAnyResourcesOperations,
    ClusterPolicySpecRulesMatchAnyResourcesSelector,
    ClusterPolicySpecRulesMutate,
    ClusterPolicySpecRulesMutateForeach,
    ClusterPolicySpecRulesMutateForeachOrder,
    ClusterPolicySpecRulesMutateForeachPreconditions,
    ClusterPolicySpecRulesMutateForeachPreconditionsAll,
    ClusterPolicySpecRulesMutateForeachPreconditionsAllOperator,
    ClusterPolicySpecValidationFailureAction,
    ClusterPolicySpecWebhookConfiguration,
    ClusterPolicySpecWebhookConfigurationMatchConditions,
)
from more_itertools import one

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import sidecar_config
from cluster.cdk8s.agentplane import sandbox_pod
from cluster.cdk8s.providers.kyverno.cluster_policy import ClusterPolicy, Validate, match_resources

LAUNCHER_LABELS = {"kubevirt.io": "virt-launcher"}
# Where a masquerade-networked guest reaches its launcher Pod, and so the relay.
GUEST_GATEWAY = "10.0.2.1"
CONTROLLER_USERNAME = "system:serviceaccount:kubevirt:kubevirt-controller"
_KUBEVIRT_API_VERSION = "kubevirt.io/v1"
_SANDBOX_API_VERSION = f"{GROUP}/{VERSION}"


def _controller(kind: str, api_version: str) -> str:
    return f"metadata.ownerReferences[?kind=='{kind}' && apiVersion=='{api_version}' && controller==`true`] | [0]"


def _context() -> list[ClusterPolicySpecRulesContext]:
    """Launcher Pod -> VMI -> VirtualMachine -> Sandbox, and the Sandbox's ServiceAccount, each read live."""
    return [
        ClusterPolicySpecRulesContext(
            name="vmiOwner",
            variable=ClusterPolicySpecRulesContextVariable(
                jmes_path=f"request.object.{_controller('VirtualMachineInstance', _KUBEVIRT_API_VERSION)}", default={}
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="vmi",
            api_call=ClusterPolicySpecRulesContextApiCall(
                url_path="/apis/kubevirt.io/v1/namespaces/{{request.namespace}}/virtualmachineinstances/{{vmiOwner.name || 'missing'}}"
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="vmOwner",
            variable=ClusterPolicySpecRulesContextVariable(
                jmes_path=f"vmi.{_controller('VirtualMachine', _KUBEVIRT_API_VERSION)}", default={}
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="vm",
            api_call=ClusterPolicySpecRulesContextApiCall(
                url_path="/apis/kubevirt.io/v1/namespaces/{{request.namespace}}/virtualmachines/{{vmOwner.name || 'missing'}}"
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="sandbox",
            variable=ClusterPolicySpecRulesContextVariable(
                jmes_path=f"vm.{_controller('Sandbox', _SANDBOX_API_VERSION)}", default={}
            ),
        ),
        ClusterPolicySpecRulesContext(
            name="account",
            api_call=ClusterPolicySpecRulesContextApiCall(
                url_path="/api/v1/namespaces/{{request.namespace}}/serviceaccounts/{{sandbox.name || 'missing'}}"
            ),
        ),
    ]


def _mismatch(key: str, value: object) -> dict[str, object]:
    return {"key": "{{ " + key + " }}", "operator": "NotEquals", "value": value}


def _token_mounts(containers: str, token_volume: str) -> str:
    return f"length(({containers} || `[]`)[].volumeMounts[] | [?name=='{token_volume}'])"


class LauncherRelayPolicy(ClusterPolicy):
    """`runner_pod` is the runner SandboxTemplate's rendered Pod template, whose egress sidecar, token
    volume and pull secrets the relay copies. `settings_config_map` holds the relay's own sidecar
    settings, which listen where the guest can reach; the runner fence keeps every other peer off
    those ports."""

    def __init__(
        self, scope: Construct, id: str, *, namespace: str, runner_pod: dict[str, Any], settings_config_map: str
    ) -> None:
        pod_spec = runner_pod["spec"]
        relay = dict(one(c for c in pod_spec["initContainers"] if c["name"] == sidecar_config.CONTAINER_NAME))
        # The launcher mutation adds this as a regular container, whose per-container restart
        # policy is not enabled on the pinned cluster. Its lifetime is owned by KubeVirt.
        relay.pop("restartPolicy")
        token_volume = sandbox_pod.EGRESS_TOKEN_VOLUME_NAME
        # A deployment that pulls without credentials (the k3s fixture) has none to copy.
        pull_secrets = {"imagePullSecrets": pod_spec["imagePullSecrets"]} if "imagePullSecrets" in pod_spec else {}
        # A runner Pod sets these Pod-wide; the launcher's own Pod security context is KubeVirt's.
        relay["securityContext"] = {
            **relay["securityContext"],
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "runAsNonRoot": True,
        }
        launchers = match_resources(
            ClusterPolicySpecRulesMatchAnyResources(
                kinds=["Pod"],
                namespaces=[namespace],
                operations=[ClusterPolicySpecRulesMatchAnyResourcesOperations.CREATE],
                selector=ClusterPolicySpecRulesMatchAnyResourcesSelector(match_labels=LAUNCHER_LABELS),
            )
        )
        updates = match_resources(
            ClusterPolicySpecRulesMatchAnyResources(
                kinds=["Pod", "Pod/ephemeralcontainers"],
                namespaces=[namespace],
                operations=[ClusterPolicySpecRulesMatchAnyResourcesOperations.UPDATE],
            )
        )
        relay_only = [
            _mismatch(_token_mounts(f"request.object.spec.containers[?name!='{relay['name']}']", token_volume), 0),
            _mismatch(_token_mounts("request.object.spec.initContainers", token_volume), 0),
        ]
        # A Pod's volumes are immutable, so only a Pod created with the token volume can be updated to mount it.
        is_launcher = " && ".join(
            f"has(object.metadata.labels) && '{key}' in object.metadata.labels && object.metadata.labels['{key}'] == '{value}'"
            for key, value in LAUNCHER_LABELS.items()
        )
        has_token_volume = f"has(object.spec.volumes) && object.spec.volumes.exists(v, v.name == '{token_volume}')"
        # The webhook is cluster-wide and the rules' `namespaces` match applies only inside Kyverno, so
        # without this an outage would fail every namespace's launchers.
        in_namespace = f"request.namespace == '{namespace}'"
        super().__init__(
            scope,
            id,
            metadata=ApiObjectMetadata(
                name=f"{namespace}-launcher-relay", annotations={"pod-policies.kyverno.io/autogen-controllers": "none"}
            ),
            background=False,
            failure_policy=ClusterPolicySpecFailurePolicy.FAIL,
            validation_failure_action=ClusterPolicySpecValidationFailureAction.ENFORCE,
            webhook_configuration=ClusterPolicySpecWebhookConfiguration(
                match_conditions=[
                    ClusterPolicySpecWebhookConfigurationMatchConditions(
                        name="launcher-or-token-volume",
                        expression=(
                            f"{in_namespace} && (request.operation == 'CREATE' ? ({is_launcher}) : ({has_token_volume}))"
                        ),
                    )
                ]
            ),
            rules=[
                ClusterPolicySpecRules(
                    name="protect-token-on-update",
                    match=updates,
                    validate=Validate.deny(
                        message="Only the launcher relay may mount its projected tokens.",
                        conditions={
                            "any": [
                                *relay_only,
                                _mismatch(_token_mounts("request.object.spec.ephemeralContainers", token_volume), 0),
                            ]
                        },
                    ),
                ),
                ClusterPolicySpecRules(
                    name="authenticate-launcher",
                    match=launchers,
                    context=_context(),
                    validate=Validate.deny(
                        message=(
                            "A launcher must be created by KubeVirt for a live VirtualMachine controlled by a "
                            "Sandbox, which also controls the ServiceAccount of the same name."
                        ),
                        conditions={
                            "any": [
                                _mismatch("request.userInfo.username", CONTROLLER_USERNAME),
                                _mismatch("vmiOwner.uid", "{{vmi.metadata.uid}}"),
                                _mismatch("vmOwner.uid", "{{vm.metadata.uid}}"),
                                _mismatch(
                                    "sandbox.uid || ''",
                                    f"{{{{account.{_controller('Sandbox', _SANDBOX_API_VERSION)}.uid || 'none'}}}}",
                                ),
                                # The guest gets no Kubernetes token of its own.
                                _mismatch("length(vmi.spec.volumes[?serviceAccount] || `[]`)", 0),
                            ]
                        },
                    ),
                ),
                ClusterPolicySpecRules(
                    name="inject-relay",
                    match=launchers,
                    context=_context(),
                    mutate=ClusterPolicySpecRulesMutate(
                        patch_strategic_merge={
                            "spec": {
                                "serviceAccountName": "{{account.metadata.name}}",
                                "automountServiceAccountToken": False,
                                **pull_secrets,
                                "containers": [relay],
                                # The subsequent daemon move appends without disturbing existing init order.
                                "+(initContainers)": [],
                                "volumes": [
                                    one(v for v in pod_spec["volumes"] if v["name"] == token_volume),
                                    {
                                        "name": sandbox_pod.SIDECAR_SETTINGS_VOLUME_NAME,
                                        "configMap": {"name": settings_config_map},
                                    },
                                ],
                            }
                        }
                    ),
                ),
                ClusterPolicySpecRules(
                    name="keep-virtiofs-through-guest-shutdown",
                    match=launchers,
                    context=_context(),
                    mutate=ClusterPolicySpecRulesMutate(
                        foreach=[
                            ClusterPolicySpecRulesMutateForeach(
                                # KubeVirt names each filesystem daemon virtiofs-<volume>. Keep the
                                # complete container, including its security/resource constraints.
                                list="request.object.spec.containers",
                                order=ClusterPolicySpecRulesMutateForeachOrder.DESCENDING,
                                # ContainerPath daemons must remain ordinary containers for
                                # KubeVirt's MissingContainerPathContainers reconciliation check.
                                preconditions=ClusterPolicySpecRulesMutateForeachPreconditions(
                                    all=[
                                        ClusterPolicySpecRulesMutateForeachPreconditionsAll(
                                            key="{{ element.command[0] || '' }}",
                                            operator=ClusterPolicySpecRulesMutateForeachPreconditionsAllOperator.EQUALS,
                                            value="/usr/libexec/virtiofsd",
                                        ),
                                        ClusterPolicySpecRulesMutateForeachPreconditionsAll(
                                            key=(
                                                "{{ contains(vmi.spec.volumes[?persistentVolumeClaim || secret || configMap]."
                                                "join('', ['virtiofs-', name]), element.name) }}"
                                            ),
                                            operator=ClusterPolicySpecRulesMutateForeachPreconditionsAllOperator.EQUALS,
                                            value=True,
                                        ),
                                    ]
                                ),
                                # Reverse indexed removals keep positions valid. Appending preserves
                                # completed initializers, unlike strategic merge's prepend behavior.
                                patches_json6902=(
                                    "- op: add\n"
                                    "  path: /spec/initContainers/-\n"
                                    '  value: {{ merge(element, `{"restartPolicy":"Always"}`) }}\n'
                                    "- op: remove\n"
                                    "  path: /spec/containers/{{elementIndex}}"
                                ),
                            )
                        ]
                    ),
                ),
                ClusterPolicySpecRules(
                    name="validate-relay",
                    match=launchers,
                    context=_context(),
                    validate=Validate.deny(
                        message="The launcher's relay identity must stay out of the compute and init containers.",
                        conditions={
                            "any": [
                                _mismatch("request.object.spec.serviceAccountName", "{{account.metadata.name}}"),
                                _mismatch("request.object.spec.automountServiceAccountToken", False),
                                _mismatch(f"length(request.object.spec.containers[?name=='{relay['name']}'])", 1),
                                *relay_only,
                            ]
                        },
                    ),
                ),
            ],
        )
