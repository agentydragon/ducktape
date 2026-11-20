"""The egress relay of a VM Sandbox. KubeVirt builds a VM's virt-launcher Pod from the VirtualMachine,
not from our SandboxTemplate's Pod, so a Kyverno `ClusterPolicy` adds the relay when the Pod is created: the
same egress sidecar, routes and workload trust files as a runner Pod's (environment_sandbox.py), copied from the
rendered runner template, with settings rendered from the template's own so the two cannot drift.

The guest reaches the relay at its masquerade gateway, `10.0.2.1`, on the ports a runner Pod's
workload dials on loopback: the HTTP proxy and the shared platform gRPC listener (platform_routes.py).
The tokens are mounted into the relay and nowhere else in the Pod, and the guest gets none. The
VirtualMachine's template carries the runner Pods' labels, which KubeVirt copies onto the launcher, so
the runner fence and the platform-route rules apply to it unchanged.

The launcher runs as the namespace's default account with no token: the relay authenticates with the
Sandbox's WorkloadCredential, as the Identity the Sandbox acts as, and the egress proxy grants it what that
Identity's bindings grant. The policy admits a launcher only when KubeVirt's controller creates it for a
live VMI of a live VirtualMachine controlled by one of our Sandboxes, whose WorkloadCredential has
accepted that VMI; that check is a CEL `ValidatingPolicy` reading those objects with `resource.Get`.

The relay is a native sidecar so the guest can flush telemetry until compute exits. It starts
after KubeVirt's existing initializers, alongside the filesystem daemons below.

PVC-, Secret- and ConfigMap-backed virtiofs daemons run as native sidecars, after KubeVirt's existing
initializers. They stay alive until compute exits, allowing the guest to close journals and unmount
state and credential volumes during shutdown. Only volumes referenced by a VMI virtiofs filesystem
device qualify; disk-only volumes do not. ContainerPath daemons remain ordinary containers because
KubeVirt reconciles them there.

Failure scope: each policy registers its own webhook, narrowed by match conditions to this namespace's
launcher creates and updates of Pods carrying the token volume. With Kyverno down, only VM Sandboxes
fail to start; nothing else in the namespace or the cluster waits on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct
from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicy,
    ClusterPolicySpec,
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
    ClusterPolicySpecWebhookConfiguration,
    ClusterPolicySpecWebhookConfigurationMatchConditions,
)
from kyverno_validatingpolicy_crds.io.kyverno.policies import (
    ValidatingPolicy,
    ValidatingPolicySpec,
    ValidatingPolicySpecEvaluation,
    ValidatingPolicySpecEvaluationAdmission,
    ValidatingPolicySpecEvaluationBackground,
    ValidatingPolicySpecFailurePolicy,
    ValidatingPolicySpecMatchConditions,
    ValidatingPolicySpecMatchConstraints,
    ValidatingPolicySpecMatchConstraintsResourceRules,
    ValidatingPolicySpecValidationActions,
    ValidatingPolicySpecValidations,
    ValidatingPolicySpecVariables,
)
from more_itertools import one

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import sidecar_config
from cluster.cdk8s.agentplane import sandbox_pod
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.providers.kyverno.cluster_policy import match_resources

LAUNCHER_LABELS = {"kubevirt.io": "virt-launcher"}
# Where a masquerade-networked guest reaches its launcher Pod, and so the relay.
GUEST_GATEWAY = "10.0.2.1"
CONTROLLER_USERNAME = "system:serviceaccount:kubevirt:kubevirt-controller"
_KUBEVIRT_API_VERSION = "kubevirt.io/v1"
_SANDBOX_API_VERSION = f"{GROUP}/{VERSION}"
_ELIGIBLE_DAEMON_NAMES = (
    "(vmi.spec.volumes[?persistentVolumeClaim || secret || configMap].join('', ['virtiofs-', name]) || `[]`)"
)
_FILESYSTEM_DAEMON_NAMES = (
    "(vmi.spec.domain.devices.filesystems[?virtiofs != null].join('', ['virtiofs-', name]) || `[]`)"
)
RUNNER_TLS_VOLUME_NAME = "agentplane-runner-relay-tls"
RUNNER_CREDENTIAL_ID_ANNOTATION = "agentplane.allegedly.works/environment-runtime-credential"
RUNNER_CREDENTIAL_VMI_UID_ANNOTATION = "agentplane.allegedly.works/environment-runtime-credential-vmi-uid"
RUNNER_CREDENTIAL_LABEL = "agentplane.allegedly.works/environment-runtime-credential"


def _controller(kind: str, api_version: str) -> str:
    return f"metadata.ownerReferences[?kind=='{kind}' && apiVersion=='{api_version}' && controller==`true`] | [0]"


def _context() -> list[ClusterPolicySpecRulesContext]:
    """Launcher Pod -> VMI -> VirtualMachine -> Sandbox, and the Sandbox's WorkloadCredential, each read live, for the mutation."""
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
            name="credential",
            api_call=ClusterPolicySpecRulesContextApiCall(
                url_path=(
                    f"/apis/{_SANDBOX_API_VERSION}/namespaces/{{{{request.namespace}}}}/workloadcredentials/"
                    "{{sandbox.name || 'missing'}}"
                )
            ),
        ),
    ]


def _cel_strings(items: Sequence[str]) -> str:
    return "[" + ", ".join(f"'{item}'" for item in items) + "]"


def _owner(kind: str, api_version: str, of: str) -> str:
    return (
        f"{of}.metadata.ownerReferences.filter(r, r.kind == '{kind}' && r.apiVersion == '{api_version}' "
        "&& has(r.controller) && r.controller == true)[0]"
    )


def _get(api_version: str, resource: str, name: str) -> str:
    return f"resource.Get('{api_version}', '{resource}', request.namespace, {name})"


def _launcher_variables(*, relay_name: str) -> list[ValidatingPolicySpecVariables]:
    """What the launcher's validation reads: the live chain Launcher Pod -> VMI -> VirtualMachine -> Sandbox, and
    the Sandbox's WorkloadCredential. A missing owner or object is a CEL error, which the policy's failure
    policy turns into a denial."""
    in_pod = "object.spec"
    devices = "variables.vmi.spec.domain.devices"
    variables = {
        "vmiOwner": _owner("VirtualMachineInstance", _KUBEVIRT_API_VERSION, "object"),
        "vmi": _get(_KUBEVIRT_API_VERSION, "virtualmachineinstances", "variables.vmiOwner.name"),
        "vmOwner": _owner("VirtualMachine", _KUBEVIRT_API_VERSION, "variables.vmi"),
        "vm": _get(_KUBEVIRT_API_VERSION, "virtualmachines", "variables.vmOwner.name"),
        "sandboxOwner": _owner("Sandbox", _SANDBOX_API_VERSION, "variables.vm"),
        "sandbox": _get(_SANDBOX_API_VERSION, "sandboxes", "variables.sandboxOwner.name"),
        "credential": _get(_SANDBOX_API_VERSION, "workloadcredentials", "variables.sandboxOwner.name"),
        "credentialOwner": _owner("Sandbox", _SANDBOX_API_VERSION, "variables.credential"),
        "vmiVolumes": "(has(variables.vmi.spec.volumes) ? variables.vmi.spec.volumes : [])",
        "initContainers": f"(has({in_pod}.initContainers) ? {in_pod}.initContainers : [])",
        # Ephemeral containers count whole: the relay is never one.
        "containersOutsideRelay": (
            f"{in_pod}.containers.filter(c, c.name != '{relay_name}') + "
            f"variables.initContainers.filter(c, c.name != '{relay_name}') + "
            f"(has({in_pod}.ephemeralContainers) ? {in_pod}.ephemeralContainers : [])"
        ),
        "virtiofsNames": (
            f"(has(variables.vmi.spec.domain) && has({devices}) && has({devices}.filesystems) "
            f"? {devices}.filesystems : []).filter(f, has(f.virtiofs)).map(f, f.name)"
        ),
        # Only a volume backed by a PVC, Secret or ConfigMap and exposed as a virtiofs device has a daemon.
        "eligibleVirtiofs": (
            "variables.vmiVolumes.filter(v, (has(v.persistentVolumeClaim) || has(v.secret) || has(v.configMap)) "
            "&& v.name in variables.virtiofsNames).map(v, v.name)"
        ),
    }
    return [ValidatingPolicySpecVariables(name=name, expression=expression) for name, expression in variables.items()]


def _launcher_validations(
    *, relay_name: str, protected_volumes: Sequence[str]
) -> list[ValidatingPolicySpecValidations]:
    on_create = "request.operation != 'CREATE' || "
    annotations = "variables.vm.metadata.annotations"
    uid = "variables.vmi.metadata.uid"
    daemon = "'virtiofs-' + n"
    native_daemons = f"variables.initContainers.filter(c, c.name == {daemon})"
    in_pod = "object.spec"
    checks = [
        (
            "Only the launcher relay may mount the runner credential and trust files.",
            "!variables.containersOutsideRelay.exists(c, has(c.volumeMounts) "
            f"&& c.volumeMounts.exists(m, m.name in {_cel_strings(protected_volumes)}))",
        ),
        (
            "A launcher must be created by KubeVirt's controller.",
            f"{on_create}request.userInfo.username == '{CONTROLLER_USERNAME}'",
        ),
        (
            "A launcher must be owned by a live VMI of a live VirtualMachine, and the guest gets no Kubernetes token.",
            f"{on_create}(variables.vmiOwner.uid == {uid} && variables.vmOwner.uid == variables.vm.metadata.uid "
            "&& !variables.vmiVolumes.exists(v, has(v.serviceAccount)))",
        ),
        (
            "The launcher's VirtualMachine must be controlled by a live Sandbox whose WorkloadCredential accepted its VMI.",
            f"{on_create}(variables.sandbox.metadata.uid == variables.sandboxOwner.uid "
            "&& variables.sandbox.metadata.name == variables.sandboxOwner.name "
            "&& variables.sandbox.status.runnerCredential.kind == 'VM' "
            f"&& variables.sandbox.status.runnerCredential.lastVmiUid == {uid} "
            "&& variables.credentialOwner.uid == variables.sandboxOwner.uid "
            f"&& variables.credential.spec.binding.vmiUID == {uid} "
            f"&& variables.credential.status.acceptedBinding.vmiUID == {uid} "
            f"&& variables.credential.status.credentialID == {annotations}['{RUNNER_CREDENTIAL_ID_ANNOTATION}'] "
            f"&& {annotations}['{RUNNER_CREDENTIAL_VMI_UID_ANNOTATION}'] == {uid} "
            f"&& variables.vmi.metadata.labels['{RUNNER_CREDENTIAL_LABEL}'] == variables.credential.status.credentialID)",
        ),
        (
            "Every PVC-, Secret-, or ConfigMap-backed VMI virtiofs daemon must be one native sidecar.",
            f"{on_create}variables.eligibleVirtiofs.all(n, "
            f"{in_pod}.containers.filter(c, c.name == {daemon}).size() == 0 "
            f"&& {native_daemons}.size() == 1 "
            f"&& {native_daemons}.filter(c, has(c.restartPolicy) && c.restartPolicy == 'Always').size() == 1 "
            f"&& {native_daemons}.exists(c, has(c.command) && c.command.size() > 0 "
            "&& c.command[0] == '/usr/libexec/virtiofsd'))",
        ),
        (
            "The launcher must have exactly one native relay init sidecar.",
            f"{on_create}({in_pod}.serviceAccountName == 'default' "
            f"&& has({in_pod}.automountServiceAccountToken) && {in_pod}.automountServiceAccountToken == false "
            f"&& {in_pod}.containers.filter(c, c.name == '{relay_name}').size() == 0 "
            f"&& variables.initContainers.filter(c, c.name == '{relay_name}').size() == 1 "
            f"&& variables.initContainers.filter(c, c.name == '{relay_name}' "
            "&& has(c.restartPolicy) && c.restartPolicy == 'Always').size() == 1)",
        ),
    ]
    return [ValidatingPolicySpecValidations(expression=expression, message=message) for message, expression in checks]


class LauncherRelayPolicy(Construct):
    """`runner_pod` is the runner SandboxTemplate's rendered Pod template, whose egress sidecar
    and pull secrets the relay copies. `settings_config_map` holds the relay's own sidecar
    settings, which listen where the guest can reach; the runner fence keeps every other peer off
    those ports.

    Mutation is a `ClusterPolicy` and validation a `ValidatingPolicy`: Kyverno runs every mutating
    webhook before any validating one, so validation sees the mutated Pod."""

    def __init__(
        self, scope: Construct, id: str, *, namespace: str, runner_pod: dict[str, Any], settings_config_map: str
    ) -> None:
        super().__init__(scope, id)
        pod_spec = runner_pod["spec"]
        relay = dict(one(c for c in pod_spec["initContainers"] if c["name"] == sidecar_config.CONTAINER_NAME))
        # A deployment that pulls without credentials (the k3s fixture) has none to copy.
        pull_secrets = {"imagePullSecrets": pod_spec["imagePullSecrets"]} if "imagePullSecrets" in pod_spec else {}
        # A runner Pod sets these Pod-wide; the launcher's own Pod security context is KubeVirt's.
        relay["securityContext"] = {
            **relay["securityContext"],
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "runAsNonRoot": True,
        }
        protected_volumes = []
        relay["volumeMounts"].append(
            {"name": RUNNER_TLS_VOLUME_NAME, "mountPath": sidecar_config.RUNNER_TLS_MOUNT_PATH, "readOnly": True}
        )
        protected_volumes.append(RUNNER_TLS_VOLUME_NAME)
        tls_volumes = [
            {
                "name": RUNNER_TLS_VOLUME_NAME,
                "projected": {
                    "defaultMode": 292,
                    "sources": [
                        {
                            "secret": {
                                "name": "{{ credential.status.secretName }}",
                                "items": [{"key": "tls.crt", "path": "tls.crt"}, {"key": "tls.key", "path": "tls.key"}],
                            }
                        },
                        {
                            "configMap": {
                                "name": cluster_ca.WORKLOAD_BUNDLE,
                                "items": [
                                    {
                                        "key": cluster_ca.BUNDLE_KEY,
                                        "path": sidecar_config.RUNNER_WORKLOAD_TRUST_FILE.name,
                                    }
                                ],
                            }
                        },
                    ],
                },
            }
        ]
        launchers = match_resources(
            ClusterPolicySpecRulesMatchAnyResources(
                kinds=["Pod"],
                namespaces=[namespace],
                operations=[ClusterPolicySpecRulesMatchAnyResourcesOperations.CREATE],
                selector=ClusterPolicySpecRulesMatchAnyResourcesSelector(match_labels=LAUNCHER_LABELS),
            )
        )
        # A Pod's volumes are immutable, so only a Pod created with the token volume can be updated to mount it.
        is_launcher = " && ".join(
            f"has(object.metadata.labels) && '{key}' in object.metadata.labels && object.metadata.labels['{key}'] == '{value}'"
            for key, value in LAUNCHER_LABELS.items()
        )
        protected_volume_predicates = [
            f"object.spec.volumes.exists(v, v.name == '{name}')" for name in protected_volumes
        ]
        has_protected_volume = "has(object.spec.volumes) && (" + " || ".join(protected_volume_predicates) + ")"
        # The webhooks are cluster-wide and the rules' `namespaces` match applies only inside Kyverno, so
        # without this an outage would fail every namespace's launchers.
        in_namespace = f"request.namespace == '{namespace}'"
        name = f"{namespace}-launcher-relay"
        ValidatingPolicy(
            self,
            "validation",
            metadata=ApiObjectMetadata(name=name),
            spec=ValidatingPolicySpec(
                validation_actions=[ValidatingPolicySpecValidationActions.DENY],
                failure_policy=ValidatingPolicySpecFailurePolicy.FAIL,
                # Reads request.userInfo and live objects, which only admission has.
                evaluation=ValidatingPolicySpecEvaluation(
                    admission=ValidatingPolicySpecEvaluationAdmission(enabled=True),
                    background=ValidatingPolicySpecEvaluationBackground(enabled=False),
                ),
                match_constraints=ValidatingPolicySpecMatchConstraints(
                    resource_rules=[
                        ValidatingPolicySpecMatchConstraintsResourceRules(
                            api_groups=[""],
                            api_versions=["v1"],
                            operations=["CREATE", "UPDATE"],
                            resources=["pods", "pods/ephemeralcontainers"],
                        )
                    ]
                ),
                match_conditions=[
                    ValidatingPolicySpecMatchConditions(name="in-namespace", expression=in_namespace),
                    ValidatingPolicySpecMatchConditions(
                        name="launcher-or-relay-credential-volume",
                        expression=f"request.operation == 'CREATE' ? ({is_launcher}) : ({has_protected_volume})",
                    ),
                ],
                variables=_launcher_variables(relay_name=relay["name"]),
                validations=_launcher_validations(relay_name=relay["name"], protected_volumes=protected_volumes),
            ),
        )
        ClusterPolicy(
            self,
            "mutation",
            metadata=ApiObjectMetadata(name=name, annotations={"pod-policies.kyverno.io/autogen-controllers": "none"}),
            spec=ClusterPolicySpec(
                background=False,
                failure_policy=ClusterPolicySpecFailurePolicy.FAIL,
                webhook_configuration=ClusterPolicySpecWebhookConfiguration(
                    match_conditions=[
                        ClusterPolicySpecWebhookConfigurationMatchConditions(
                            name="launcher", expression=f"{in_namespace} && ({is_launcher})"
                        )
                    ]
                ),
                rules=[
                    ClusterPolicySpecRules(
                        name="inject-relay",
                        match=launchers,
                        context=_context(),
                        mutate=ClusterPolicySpecRulesMutate(
                            patch_strategic_merge={
                                "spec": {
                                    "serviceAccountName": "default",
                                    "automountServiceAccountToken": False,
                                    **pull_secrets,
                                    # The subsequent daemon move appends without disturbing existing init order.
                                    "+(initContainers)": [],
                                    "volumes": [
                                        {
                                            "name": sandbox_pod.SIDECAR_SETTINGS_VOLUME_NAME,
                                            "configMap": {"name": settings_config_map},
                                        },
                                        *tls_volumes,
                                    ],
                                }
                            }
                        ),
                    ),
                    ClusterPolicySpecRules(
                        name="append-native-relay",
                        match=launchers,
                        # Keep the image structured for Kustomize's image-pins transformer.
                        context=[
                            ClusterPolicySpecRulesContext(
                                name="relay", variable=ClusterPolicySpecRulesContextVariable(value=relay)
                            )
                        ],
                        preconditions={
                            "all": [
                                {
                                    "key": (
                                        "{{ (request.object.spec.initContainers || `[]`)"
                                        f"[?name=='{relay['name']}'] | length(@)"
                                        " }}"
                                    ),
                                    "operator": "Equals",
                                    "value": 0,
                                }
                            ]
                        },
                        mutate=ClusterPolicySpecRulesMutate(
                            patches_json6902="- op: add\n  path: /spec/initContainers/-\n  value: {{ to_string(relay) }}\n"
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
                                                    "{{ contains("
                                                    + _ELIGIBLE_DAEMON_NAMES
                                                    + ", element.name) && contains("
                                                    + _FILESYSTEM_DAEMON_NAMES
                                                    + ", element.name) }}"
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
                ],
            ),
        )
        # The built-in view role does not cover Agentplane's CRDs. The injection reads the Sandbox's
        # WorkloadCredential to name the accepted Secret; it never reads Secrets.
        reader = Role(
            self,
            "sandbox-reader",
            metadata=ApiObjectMetadata(name="launcher-relay-sandbox-reader", namespace=namespace),
            rules=[
                RolePolicyRule(
                    resources=[custom_resource(GROUP, "sandboxes"), custom_resource(GROUP, "workloadcredentials")],
                    verbs=["get"],
                )
            ],
        )
        RoleBinding(
            self,
            "sandbox-reader-binding",
            metadata=ApiObjectMetadata(name="launcher-relay-sandbox-reader", namespace=namespace),
            role=reader,
        ).add_subjects(
            ServiceAccount.from_service_account_name(
                self, "kyverno-admission", "kyverno-admission-controller", namespace_name="kyverno"
            )
        )
