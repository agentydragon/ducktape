"""Constrain staging's exception to Pod Security Admission for its Firecracker launcher."""

from collections.abc import Sequence

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch
from cdk8s_plus_34 import k8s
from constructs import Construct

from agentplane.crd_group import GROUP, VERSION
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane.environment_firecracker import BOOT_IMAGE, LAUNCHER_IMAGE, NAME
from cluster.cdk8s.agentplane.environment_vm import RUNNER_DISK_IMAGE

_BACKEND = f"{GROUP}/environment-backend"
_LAUNCHER_REPOSITORY = LAUNCHER_IMAGE.rpartition(":")[0]
_RELAY_REPOSITORY = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
_LAUNCHER_CONFIG = f"{NAME}-launcher"
_RELAY_CONFIG = f"{NAME}-relay"
_GUEST_CONFIG = f"{NAME}-config"
_LAUNCHER_MOUNTS = {
    "firecracker-settings": "/run/agentplane/firecracker",
    "guest-config": "/run/agentplane/config",
    "guest-trust": "/run/agentplane/trust",
    "tun": "/dev/net/tun",
    "agentplane-launch": "/run/agentplane/sandbox",
    "agentplane-environment-runtime-tls": "/run/agentplane/environment-runtime-tls",
    "agentplane-firecracker-intent": "/run/agentplane/firecracker-intent",
    "agentplane-firecracker-boot": "/images/boot",
    "agentplane-firecracker-runner": "/images/runner",
    "state": "/var/lib/agentplane/state",
    "workspace": "/var/lib/agentplane/workspace",
}
_RELAY_MOUNTS = {
    "egress-sidecar-settings": "/etc/agentplane-egress-sidecar",
    "agentplane-environment-runtime-tls": "/run/agentplane/environment-runtime-tls",
}
_CONFIG_MAP_VOLUMES = {
    "egress-ca": "agentplane-egress-ca",
    "tool-config": "sandbox-tool-config",
    "egress-sidecar-settings": _RELAY_CONFIG,
    "firecracker-settings": _LAUNCHER_CONFIG,
    "guest-config": _GUEST_CONFIG,
    "guest-trust": "agentplane-egress-ca",
}


def _username(account: ServiceAccountRef) -> str:
    return f"system:serviceaccount:{account.namespace}:{account.name}"


def _image(reference: str, repository: str) -> str:
    return f"{reference}.startsWith('{repository}:') && {reference}.matches(':devel-[0-9]{{14}}-[0-9a-f]{{7}}$')"


def _mounts(container: str, mounts: dict[str, str]) -> str:
    return (
        f"{container}.?volumeMounts.orValue([]).all(m, m.name in {mounts!r} && m.mountPath == {mounts!r}[m.name]"
        " && m.?mountPropagation.orValue('None') == 'None' && !has(m.subPath) && !has(m.subPathExpr))"
    )


def _add_policy(
    scope: Construct,
    name: str,
    *,
    operations: list[str],
    resources: list[str],
    conditions: Sequence[k8s.MatchCondition],
    validations: Sequence[k8s.Validation],
    variables: Sequence[k8s.Variable] = (),
) -> None:
    k8s.KubeValidatingAdmissionPolicy(
        scope,
        name,
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicySpec(
            failure_policy="Fail",
            match_constraints=k8s.MatchResources(
                resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=[""], api_versions=["v1"], operations=operations, resources=resources
                    )
                ]
            ),
            match_conditions=list(conditions),
            validations=list(validations),
            variables=list(variables),
        ),
    )
    k8s.KubeValidatingAdmissionPolicyBinding(
        scope,
        f"{name}-binding",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicyBindingSpec(policy_name=name, validation_actions=["Deny"]),
    )


def add_firecracker_admission(
    scope: Construct, controller: ServiceAccountRef, *, actions: ServiceAccountRef, command_container: str
) -> None:
    namespace = controller.namespace
    in_namespace = k8s.MatchCondition(name="staging", expression=f"request.namespace == '{namespace}'")
    controller_request = f"request.userInfo.username == '{_username(controller)}'"
    launcher_selector = {"matchLabels": {_BACKEND: "firecracker"}}
    touches_launcher = (
        f"object.?metadata.?labels[?'{_BACKEND}'].orValue('') == 'firecracker'"
        f" || (oldObject != null && oldObject.?metadata.?labels[?'{_BACKEND}'].orValue('') == 'firecracker')"
    )
    # This built-in Kyverno rule preserves the baseline checks for the controller after its
    # username is exempted from Talos PSA. The native policy below guards the launcher's exceptions.
    baseline = {"level": "baseline", "version": "latest"}
    policy = ApiObject(
        scope,
        "firecracker-controller-baseline",
        api_version="kyverno.io/v1",
        kind="Policy",
        metadata=ApiObjectMetadata(
            name="firecracker-controller-baseline",
            namespace=namespace,
            annotations={"pod-policies.kyverno.io/autogen-controllers": "none"},
        ),
    )
    pods = {"kinds": ["Pod"]}
    launcher = {"resources": {**pods, "selector": launcher_selector}}
    policy.add_json_patch(
        JsonPatch.add(
            "/spec",
            {
                "background": False,
                "webhookConfiguration": {
                    "failurePolicy": "Fail",
                    "matchConditions": [
                        {
                            "name": "staging-controller",
                            "expression": f"{in_namespace.expression} && {controller_request}",
                        }
                    ],
                },
                "rules": [
                    {
                        "name": "staging-controller-baseline",
                        "match": {"any": [{"resources": pods}]},
                        "exclude": {"any": [launcher]},
                        "validate": {"failureAction": "Enforce", "podSecurity": baseline},
                    },
                    {
                        "name": "firecracker-launcher-baseline",
                        "match": {"any": [launcher]},
                        "validate": {
                            "failureAction": "Enforce",
                            "podSecurity": {
                                **baseline,
                                "exclude": [
                                    {
                                        "controlName": "Privileged Containers",
                                        "images": [f"{_LAUNCHER_REPOSITORY}:devel-*"],
                                    },
                                    {
                                        "controlName": "HostPath Volumes",
                                        "restrictedField": "spec.volumes[*].hostPath",
                                        "values": ["/dev/net/tun"],
                                    },
                                ],
                            },
                        },
                    },
                ],
            },
        )
    )
    _add_policy(
        scope,
        "agentplane-staging-firecracker-launcher",
        operations=["CREATE", "UPDATE"],
        resources=["pods", "pods/ephemeralcontainers"],
        conditions=[in_namespace, k8s.MatchCondition(name="firecracker", expression=touches_launcher)],
        variables=[
            k8s.Variable(name="pod", expression="object.spec"),
            k8s.Variable(name="launcher", expression="variables.pod.containers[0]"),
            k8s.Variable(name="relay", expression="variables.pod.initContainers[0]"),
            k8s.Variable(name="owners", expression="object.metadata.?ownerReferences.orValue([])"),
            k8s.Variable(name="labels", expression="object.metadata.?labels.orValue({})"),
        ],
        validations=[
            k8s.Validation(
                expression=(
                    f"{controller_request} && request.subResource == ''"
                    " && size(variables.owners) == 1 && variables.owners.all(o,"
                    f" o.apiVersion == '{GROUP}/{VERSION}' && o.kind == 'Sandbox'"
                    " && o.name == object.metadata.name && o.?controller.orValue(false) && o.uid != '')"
                    f" && variables.labels[?'{_BACKEND}'].orValue('') == 'firecracker'"
                    f" && variables.labels[?'{GROUP}/sandbox'].orValue('') == object.metadata.name"
                ),
                message="The staging Environments controller alone writes Sandbox-owned Firecracker launcher Pods.",
            ),
            k8s.Validation(
                expression=(
                    "!variables.pod.?hostNetwork.orValue(false) && !variables.pod.?hostPID.orValue(false)"
                    " && !variables.pod.?hostIPC.orValue(false) && !variables.pod.?shareProcessNamespace.orValue(false)"
                    " && !variables.pod.?automountServiceAccountToken.orValue(true)"
                    " && variables.pod.serviceAccountName == 'default' && variables.pod.restartPolicy == 'Never'"
                    " && variables.pod.?ephemeralContainers.orValue([]).size() == 0"
                ),
                message="A Firecracker launcher uses Pod namespaces and runs without a service token or ephemeral containers.",
            ),
            k8s.Validation(
                expression=(
                    "size(variables.pod.containers) == 1 && variables.launcher.name == 'environment-runtime'"
                    f" && ({_image('variables.launcher.image', _LAUNCHER_REPOSITORY)})"
                    " && variables.launcher.?command.orValue([]).size() == 0"
                    " && variables.launcher.args == ['--config', '/run/agentplane/firecracker/config.json']"
                    " && variables.launcher.?securityContext.?privileged.orValue(false)"
                    " && !has(variables.launcher.lifecycle) && !has(variables.launcher.startupProbe)"
                    " && !has(variables.launcher.livenessProbe) && !variables.launcher.?readinessProbe.?exec.hasValue()"
                    " && variables.launcher.?envFrom.orValue([]).size() == 0"
                    " && variables.launcher.?volumeDevices.orValue([]).size() == 0"
                    " && variables.launcher.?env.orValue([]).all(e,"
                    " (e.name == 'FIRECRACKER_CONFIG_FILE' && e.?value.orValue('') == '/run/agentplane/firecracker/config.json')"
                    " || (e.name == 'POD_UID' && e.?valueFrom.?fieldRef.?fieldPath.orValue('') == 'metadata.uid')"
                    " || (e.name == 'SANDBOX_UID' && e.?value.orValue('') == variables.owners[0].uid)"
                    " || (e.name in ['FIRECRACKER_VCPU_COUNT', 'FIRECRACKER_MEMORY_MIB'] && e.?value.orValue('').matches('^[0-9]+$'))"
                    f" || (e.name == 'FIRECRACKER_BOOT_IMAGE' && ({_image('e.value', BOOT_IMAGE.rpartition(':')[0])}))"
                    f" || (e.name == 'FIRECRACKER_RUNNER_IMAGE' && ({_image('e.value', RUNNER_DISK_IMAGE.rpartition(':')[0])}))"
                    " || (e.name == 'WORKSPACE_KIND' && e.?value.orValue('') == 'RawDiskImage'))"
                    f" && ({_mounts('variables.launcher', _LAUNCHER_MOUNTS)})"
                ),
                message="The privileged container uses the published Firecracker supervisor, its entrypoint and reviewed inputs.",
            ),
            k8s.Validation(
                expression=(
                    "size(variables.pod.?initContainers.orValue([])) == 1 && variables.relay.name == 'egress-sidecar'"
                    f" && ({_image('variables.relay.image', _RELAY_REPOSITORY)})"
                    " && variables.relay.?restartPolicy.orValue('') == 'Always'"
                    " && !variables.relay.?securityContext.?privileged.orValue(false)"
                    " && variables.relay.?securityContext.?runAsNonRoot.orValue(false)"
                    " && !variables.relay.?securityContext.?allowPrivilegeEscalation.orValue(true)"
                    " && variables.relay.?command.orValue([]).size() == 0 && variables.relay.?args.orValue([]).size() == 0"
                    " && !has(variables.relay.lifecycle) && !has(variables.relay.startupProbe)"
                    " && !has(variables.relay.livenessProbe) && !variables.relay.?readinessProbe.?exec.hasValue()"
                    " && variables.relay.?envFrom.orValue([]).size() == 0"
                    " && variables.relay.?env.orValue([]).all(e, e.name == 'AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE'"
                    " && e.?value.orValue('') == '/etc/agentplane-egress-sidecar/settings.yaml')"
                    f" && ({_mounts('variables.relay', _RELAY_MOUNTS)})"
                ),
                message="The Firecracker egress sidecar uses the published image, its fixed settings path and no privileges.",
            ),
            k8s.Validation(
                expression=(
                    f"variables.pod.?volumes.orValue([]).all(v, (v.name in {_CONFIG_MAP_VOLUMES!r}"
                    f" && v.?configMap.?name.orValue('') == {_CONFIG_MAP_VOLUMES!r}[v.name])"
                    " || (v.name == 'tun' && v.?hostPath.?path.orValue('') == '/dev/net/tun' && v.?hostPath.?type.orValue('') == 'CharDevice')"
                    " || (v.name == 'agentplane-launch' && v.?configMap.?name.orValue('') == object.metadata.name + '-launch')"
                    " || (v.name in ['state', 'workspace'] && v.?persistentVolumeClaim.?claimName.orValue('') == object.metadata.name + '-' + v.name)"
                    " || (v.name == 'agentplane-firecracker-intent' && has(v.downwardAPI))"
                    f" || (v.name == 'agentplane-firecracker-boot' && has(v.image) && ({_image('v.image.reference', BOOT_IMAGE.rpartition(':')[0])}))"
                    f" || (v.name == 'agentplane-firecracker-runner' && has(v.image) && ({_image('v.image.reference', RUNNER_DISK_IMAGE.rpartition(':')[0])}))"
                    " || (v.name == 'agentplane-environment-runtime-tls' && has(v.projected) && v.projected.sources.all(p,"
                    " p.?configMap.?name.orValue('') in ['cluster-internal-ca-bundle', 'agentplane-workload-ca-bundle']"
                    " || p.?secret.?name.orValue('').matches('^svid-[a-z0-9.-]+-[0-9a-f]{32}$'))))"
                ),
                message="Firecracker volumes are limited to the tun device, reviewed inputs and Sandbox-owned state.",
            ),
        ],
    )
    _add_policy(
        scope,
        "agentplane-staging-firecracker-config",
        operations=["CREATE", "UPDATE"],
        resources=["configmaps"],
        conditions=[
            in_namespace,
            k8s.MatchCondition(name="environments", expression=controller_request),
            k8s.MatchCondition(
                name="launcher-config",
                expression=f"object.metadata.name in {[_LAUNCHER_CONFIG, _RELAY_CONFIG, _GUEST_CONFIG]!r}",
            ),
        ],
        validations=[
            k8s.Validation(expression="false", message="Firecracker launcher configuration is deployment-owned.")
        ],
    )
    _add_policy(
        scope,
        "agentplane-staging-actions-command-exec",
        operations=["CONNECT"],
        resources=["pods/exec"],
        conditions=[
            in_namespace,
            k8s.MatchCondition(name="actions", expression=f"request.userInfo.username == '{_username(actions)}'"),
        ],
        validations=[
            k8s.Validation(
                expression=f"object.?container.orValue('') == '{command_container}'",
                message="Actions must explicitly select the command box container in pods/exec requests.",
            )
        ],
    )
