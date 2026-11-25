"""The egress relay of a VM Sandbox. KubeVirt builds a VM's virt-launcher Pod from the VirtualMachine,
not from our SandboxTemplate's Pod, so a Kyverno `MutatingPolicy` adds the relay when the Pod is created: the
same egress sidecar, routes and workload trust files as a runner Pod's (environment_sandbox.py), copied at
admission from the runner SandboxTemplate, so the sidecar image stays pinned where the template's is.

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
launcher creates (and, for validation, updates of Pods carrying the token volume). With Kyverno down, only VM Sandboxes
fail to start; nothing else in the namespace or the cluster waits on it.
"""

from __future__ import annotations

import json

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct
from kyverno_mutatingpolicy_crds.io.kyverno.policies import (
    MutatingPolicy,
    MutatingPolicySpec,
    MutatingPolicySpecAutogen,
    MutatingPolicySpecAutogenPodControllers,
    MutatingPolicySpecEvaluation,
    MutatingPolicySpecEvaluationAdmission,
    MutatingPolicySpecEvaluationBackground,
    MutatingPolicySpecFailurePolicy,
    MutatingPolicySpecMatchConditions,
    MutatingPolicySpecMatchConstraints,
    MutatingPolicySpecMatchConstraintsResourceRules,
    MutatingPolicySpecMutations,
    MutatingPolicySpecMutationsJsonPatch,
    MutatingPolicySpecVariables,
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

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import sidecar_config
from cluster.cdk8s.agentplane import sandbox_pod
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.cert_manager import cluster_ca

LAUNCHER_LABELS = {"kubevirt.io": "virt-launcher"}
# Where a masquerade-networked guest reaches its launcher Pod, and so the relay.
GUEST_GATEWAY = "10.0.2.1"
CONTROLLER_USERNAME = "system:serviceaccount:kubevirt:kubevirt-controller"
_KUBEVIRT_API_VERSION = "kubevirt.io/v1"
_SANDBOX_API_VERSION = f"{GROUP}/{VERSION}"
RUNNER_TLS_VOLUME_NAME = "agentplane-runner-relay-tls"
RUNNER_CREDENTIAL_ID_ANNOTATION = "agentplane.allegedly.works/environment-runtime-credential"
RUNNER_CREDENTIAL_VMI_UID_ANNOTATION = "agentplane.allegedly.works/environment-runtime-credential-vmi-uid"
RUNNER_CREDENTIAL_LABEL = "agentplane.allegedly.works/environment-runtime-credential"


def _owner(kind: str, api_version: str, of: str) -> str:
    return (
        f"{of}.metadata.ownerReferences.filter(r, r.kind == '{kind}' && r.apiVersion == '{api_version}' "
        "&& has(r.controller) && r.controller == true)[0]"
    )


def _get(api_version: str, resource: str, name: str) -> str:
    return f"resource.Get('{api_version}', '{resource}', request.namespace, {name})"


def _launcher_variables(*, relay_name: str) -> dict[str, str]:
    """What the launcher's validation reads: the live chain Launcher Pod -> VMI -> VirtualMachine -> Sandbox, and
    the Sandbox's WorkloadCredential. A missing owner or object is a CEL error, which the policy's failure
    policy turns into a denial."""
    in_pod = "object.spec"
    devices = "variables.vmi.spec.domain.devices"
    return {
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


def _launcher_validations(*, relay_name: str) -> list[ValidatingPolicySpecValidations]:
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
            f"&& c.volumeMounts.exists(m, m.name == '{RUNNER_TLS_VOLUME_NAME}'))",
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


def _constant(value: object) -> str:
    """A CEL expression for a constant of mixed types, which a CEL literal cannot type-check."""
    text = json.dumps(value, separators=(",", ":"))
    if "'" in text or "\\" in text:
        raise ValueError(f"{text=} cannot be quoted in CEL")
    return f"json.unmarshal('{text}')"


def _patch(op: str, path: str, *, value: str | None = None, source: str | None = None) -> str:
    """A CEL `JSONPatch`; `path`, `value` and `source` are CEL expressions."""
    fields = [f"op: '{op}'", f"path: {path}"]
    if source is not None:
        fields.append(f"from: {source}")
    if value is not None:
        fields.append(f"value: {value}")
    return "JSONPatch{" + ", ".join(fields) + "}"


def _count(field: str) -> str:
    return f"(has(object.spec.{field}) ? size(object.spec.{field}) : 0)"


def _append_named(field: str, name: str, value: str, *, then: str = "[]") -> str:
    """Operations appending `value`, the item named `name`, to the Pod's `field` list unless it is there,
    then `then`, operations that may address the new item at `NEW`. The index is read from the Pod as it
    stands when the mutation starts, so a mutation appends one item."""
    items = f"object.spec.{field}"
    add = (
        f"has({items}) ? [{_patch('add', f"'/spec/{field}/-'", value=value)}] "
        f": [{_patch('add', f"'/spec/{field}'", value=f'[{value}]')}]"
    )
    new_index = f"string({_count(field)})"
    return (
        f"(has({items}) && {items}.exists(x, x.name == '{name}')) ? [] : ({add}) + ({then.replace('NEW', new_index)})"
    )


def _mutations(*, relay_name: str, settings_config_map: str, tls_secret: str) -> list[MutatingPolicySpecMutations]:
    """In order: Pod-wide fields, the two volumes, the relay container, then the filesystem daemons.
    `tls_secret` is a CEL expression for the name of the Secret the runner credential is in. Each is a
    no-op once applied: Kyverno runs again when a later webhook mutates the Pod."""
    pod = "variables.templatePod"
    init = _count("initContainers")
    relay_path = f"'/spec/initContainers/' + string({init})"
    settings_volume = {"name": sandbox_pod.SIDECAR_SETTINGS_VOLUME_NAME, "configMap": {"name": settings_config_map}}
    secret_source = {
        "secret": {
            "name": "set below",
            "items": [{"key": "tls.crt", "path": "tls.crt"}, {"key": "tls.key", "path": "tls.key"}],
        }
    }
    trust_source = {
        "configMap": {
            "name": cluster_ca.WORKLOAD_BUNDLE,
            "items": [{"key": cluster_ca.BUNDLE_KEY, "path": sidecar_config.RUNNER_WORKLOAD_TRUST_FILE.name}],
        }
    }
    tls_volume = {
        "name": RUNNER_TLS_VOLUME_NAME,
        "projected": {"defaultMode": 292, "sources": [secret_source, trust_source]},
    }
    tls_mount = {"name": RUNNER_TLS_VOLUME_NAME, "mountPath": sidecar_config.RUNNER_TLS_MOUNT_PATH, "readOnly": True}
    # A deployment that pulls without credentials (the k3s fixture) has none to copy.
    pull_secrets = (
        f"has({pod}.imagePullSecrets) ? (has(object.spec.imagePullSecrets) "
        f"? {pod}.imagePullSecrets.filter(s, !object.spec.imagePullSecrets.exists(e, e.name == s.name))"
        f".map(s, {_patch('add', "'/spec/imagePullSecrets/-'", value='s')}) "
        f": [{_patch('add', "'/spec/imagePullSecrets'", value=f'{pod}.imagePullSecrets')}]) : []"
    )
    pod_fields = (
        f"[{_patch('add', "'/spec/serviceAccountName'", value="'default'")}, "
        f"{_patch('add', "'/spec/automountServiceAccountToken'", value='false')}] + ({pull_secrets})"
    )
    tls_secret_name = _patch("replace", "'/spec/volumes/' + NEW + '/projected/sources/0/secret/name'", value=tls_secret)
    # A runner Pod sets these Pod-wide; the launcher's own Pod security context is KubeVirt's.
    relay_ops = ", ".join(
        [
            _patch("add", "'/spec/initContainers/-'", value="variables.templateRelay"),
            *(
                _patch("add", f"{relay_path} + '/securityContext/{field}'", value=value)
                for field, value in [("runAsUser", "1000"), ("runAsGroup", "1000"), ("runAsNonRoot", "true")]
            ),
            _patch("add", f"{relay_path} + '/volumeMounts/-'", value=_constant(tls_mount)),
        ]
    )
    relay = (
        f"(has(object.spec.initContainers) && object.spec.initContainers.exists(c, c.name == '{relay_name}')) ? [] : "
        f"(has(object.spec.initContainers) ? [] : [{_patch('add', "'/spec/initContainers'", value='[]')}]) "
        f"+ [{relay_ops}]"
    )
    # Reverse indexed moves keep positions valid. A daemon keeps its complete container, constraints
    # included. Moving appends, preserving completed initializers, unlike a merge's prepend; ContainerPath
    # daemons stay ordinary containers for KubeVirt's MissingContainerPathContainers reconciliation check.
    daemon_indexes = (
        "lists.range(size(object.spec.containers)).filter(i, "
        "has(object.spec.containers[i].command) && object.spec.containers[i].command.size() > 0 "
        "&& object.spec.containers[i].command[0] == '/usr/libexec/virtiofsd' "
        "&& object.spec.containers[i].name in variables.eligibleVirtiofs.map(n, 'virtiofs-' + n)).reverse()"
    )
    daemons = (
        f"{daemon_indexes}.map(i, {_patch('move', "'/spec/initContainers/-'", source="'/spec/containers/' + string(i)")}) "
        f"+ lists.range(size({daemon_indexes})).map(k, "
        f"{_patch('add', f"'/spec/initContainers/' + string({init} + k) + '/restartPolicy'", value="'Always'")})"
    )
    expressions = [
        pod_fields,
        _append_named("volumes", sandbox_pod.SIDECAR_SETTINGS_VOLUME_NAME, _constant(settings_volume)),
        _append_named("volumes", RUNNER_TLS_VOLUME_NAME, _constant(tls_volume), then=f"[{tls_secret_name}]"),
        relay,
        daemons,
    ]
    return [
        MutatingPolicySpecMutations(
            patch_type="JSONPatch", json_patch=MutatingPolicySpecMutationsJsonPatch(expression=expression)
        )
        for expression in expressions
    ]


class LauncherRelayPolicy(Construct):
    """`template_name` is the runner SandboxTemplate, whose egress sidecar and pull secrets the relay copies
    at admission, so its image is pinned where the template's is. `settings_config_map` holds the relay's own
    sidecar settings, which listen where the guest can reach; the runner fence keeps every other peer off
    those ports.

    Mutation is a `MutatingPolicy` and validation a `ValidatingPolicy`: Kyverno runs every mutating
    webhook before any validating one, so validation sees the mutated Pod."""

    def __init__(
        self, scope: Construct, id: str, *, namespace: str, template_name: str, settings_config_map: str
    ) -> None:
        super().__init__(scope, id)
        relay_name = sidecar_config.CONTAINER_NAME
        # A Pod's volumes are immutable, so only a Pod created with the token volume can be updated to mount it.
        is_launcher = " && ".join(
            f"has(object.metadata.labels) && '{key}' in object.metadata.labels && object.metadata.labels['{key}'] == '{value}'"
            for key, value in LAUNCHER_LABELS.items()
        )
        has_protected_volume = (
            f"has(object.spec.volumes) && object.spec.volumes.exists(v, v.name == '{RUNNER_TLS_VOLUME_NAME}')"
        )
        # The webhooks are cluster-wide and the rules' `namespaces` match applies only inside Kyverno, so
        # without this an outage would fail every namespace's launchers.
        in_namespace = f"request.namespace == '{namespace}'"
        name = f"{namespace}-launcher-relay"
        variables = _launcher_variables(relay_name=relay_name)
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
                variables=[
                    ValidatingPolicySpecVariables(name=variable, expression=expression)
                    for variable, expression in variables.items()
                ],
                validations=_launcher_validations(relay_name=relay_name),
            ),
        )
        template_variables = {
            "templatePod": _get(_SANDBOX_API_VERSION, "sandboxtemplates", repr(template_name)) + ".spec.pod.spec",
            "templateRelay": f"variables.templatePod.initContainers.filter(c, c.name == '{relay_name}')[0]",
        }
        MutatingPolicy(
            self,
            "mutation",
            metadata=ApiObjectMetadata(name=name),
            spec=MutatingPolicySpec(
                # Launchers are Pods; no controller of them is ours.
                autogen=MutatingPolicySpecAutogen(
                    pod_controllers=MutatingPolicySpecAutogenPodControllers(controllers=[])
                ),
                failure_policy=MutatingPolicySpecFailurePolicy.FAIL,
                reinvocation_policy="IfNeeded",
                evaluation=MutatingPolicySpecEvaluation(
                    admission=MutatingPolicySpecEvaluationAdmission(enabled=True),
                    background=MutatingPolicySpecEvaluationBackground(enabled=False),
                ),
                match_constraints=MutatingPolicySpecMatchConstraints(
                    resource_rules=[
                        MutatingPolicySpecMatchConstraintsResourceRules(
                            api_groups=[""], api_versions=["v1"], operations=["CREATE"], resources=["pods"]
                        )
                    ]
                ),
                match_conditions=[
                    MutatingPolicySpecMatchConditions(name="in-namespace", expression=in_namespace),
                    MutatingPolicySpecMatchConditions(name="launcher", expression=is_launcher),
                ],
                variables=[
                    MutatingPolicySpecVariables(name=variable, expression=expression)
                    for variable, expression in {**variables, **template_variables}.items()
                ],
                mutations=_mutations(
                    relay_name=relay_name,
                    settings_config_map=settings_config_map,
                    tls_secret="variables.credential.status.secretName",
                ),
            ),
        )
        # The built-in view role does not cover Agentplane's CRDs. The policies read the Sandbox, its
        # runner SandboxTemplate and its WorkloadCredential; the injection names the accepted Secret but never reads Secrets.
        reader = Role(
            self,
            "sandbox-reader",
            metadata=ApiObjectMetadata(name="launcher-relay-sandbox-reader", namespace=namespace),
            rules=[
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, "sandboxes"),
                        custom_resource(GROUP, "sandboxtemplates"),
                        custom_resource(GROUP, "workloadcredentials"),
                    ],
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
