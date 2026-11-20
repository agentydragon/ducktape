"""The launcher relay policies in the real Kyverno CLI: the mutation, then the validation of the mutated Pod.
The CLI loads no API reads and cannot map Agentplane's or KubeVirt's kinds for `resource.Get`, so the test
supplies the VMI, VirtualMachine, Sandbox and WorkloadCredential the policies would read; the ownership
checks still run. `kyverno apply` has no operation flag, so an UPDATE is validated as the CREATE
`request.operation` check read with the operation replaced."""

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting  # Testing aliased so pytest does not collect it
from more_itertools import one

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import sidecar_config
from cluster.cdk8s.agentplane.launcher_relay import (
    CONTROLLER_USERNAME,
    LAUNCHER_LABELS,
    RUNNER_CREDENTIAL_ID_ANNOTATION,
    RUNNER_CREDENTIAL_LABEL,
    RUNNER_CREDENTIAL_VMI_UID_ANNOTATION,
    RUNNER_TLS_VOLUME_NAME,
    LauncherRelayPolicy,
)
from cluster.cdk8s.agentplane.sandbox_pod import SIDECAR_SETTINGS_VOLUME_NAME
from cluster.cdk8s.kyverno.apply import KyvernoApplyResult, apply_policy

NAMESPACE = "launcher-relay-test"
RELAY_SETTINGS = "relay-test-relay-settings"
RUNNER_CREDENTIAL_ID = "0123456789abcdef0123456789abcdef"
RUNNER_SECRET = f"svid-sandbox-a-{RUNNER_CREDENTIAL_ID}"
RUNNER_POD = {
    "metadata": {"labels": {"app": "relay-test-runner"}},
    "spec": {
        "imagePullSecrets": [{"name": "relay-test-pull"}],
        "containers": [
            {"name": "runner", "image": "runner:test", "volumeMounts": [{"name": "state", "mountPath": "/state"}]}
        ],
        "initContainers": [
            {
                "name": "egress-sidecar",
                "restartPolicy": "Always",
                "image": "sidecar:test",
                "env": [{"name": "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE", "value": "/etc/relay-test/settings.yaml"}],
                "securityContext": {"allowPrivilegeEscalation": False},
                "volumeMounts": [
                    {"name": SIDECAR_SETTINGS_VOLUME_NAME, "mountPath": "/etc/relay-test", "readOnly": True}
                ],
            }
        ],
        "volumes": [
            {"name": "unrelated", "configMap": {"name": "relay-test-config"}},
            {"name": SIDECAR_SETTINGS_VOLUME_NAME, "configMap": {"name": "relay-test-settings"}},
        ],
    },
}


def owner(api_version: str, kind: str, uid: str) -> dict:
    return {"apiVersion": api_version, "kind": kind, "name": "sandbox-a", "uid": uid, "controller": True}


@dataclass(frozen=True)
class Policies:
    mutation: dict
    validation: dict


@pytest.fixture(scope="module")
def policy() -> Policies:
    chart = Chart(App(), "test")
    LauncherRelayPolicy(
        chart, "policy", namespace=NAMESPACE, template_name="relay-test-template", settings_config_map=RELAY_SETTINGS
    )
    documents = Cdk8sTesting.synth(chart)
    mutation: dict = one(doc for doc in documents if doc["kind"] == "MutatingPolicy")
    validation: dict = one(doc for doc in documents if doc["kind"] == "ValidatingPolicy")
    # The CLI's policy validation needs a cluster to check match conditions against and panics
    # without one; they narrow which requests reach the policy, not what it decides.
    del mutation["spec"]["matchConditions"]
    del validation["spec"]["matchConditions"]
    return Policies(mutation=mutation, validation=validation)


def test_tls_injection_can_read_only_the_sandbox_and_its_credential_in_its_namespace() -> None:
    chart = Chart(App(), "test-tls-reader")
    LauncherRelayPolicy(
        chart, "policy", namespace=NAMESPACE, template_name="relay-test-template", settings_config_map=RELAY_SETTINGS
    )
    documents = Cdk8sTesting.synth(chart)
    role = one(doc for doc in documents if doc["kind"] == "Role")
    assert role["metadata"]["namespace"] == NAMESPACE
    assert role["rules"] == [
        {"apiGroups": [GROUP], "resources": ["sandboxes"], "verbs": ["get"]},
        {"apiGroups": [GROUP], "resources": ["sandboxtemplates"], "verbs": ["get"]},
        {"apiGroups": [GROUP], "resources": ["workloadcredentials"], "verbs": ["get"]},
    ]
    binding = one(doc for doc in documents if doc["kind"] == "RoleBinding")
    assert binding["metadata"]["namespace"] == NAMESPACE
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": role["metadata"]["name"],
    }
    assert binding["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": "kyverno-admission-controller", "namespace": "kyverno"}
    ]


def reads(
    *,
    vm_uid: str,
    vm_owner_kind: str,
    vmi_volumes: list[dict] | None,
    vmi_filesystems: list[dict] | None,
    credential_kind: str = "VM",
    record_credential_id: str = RUNNER_CREDENTIAL_ID,
    record_owner_uid: str = "sb-uid",
    binding_vmi_uid: str = "vmi-uid",
    accepted_vmi_uid: str = "vmi-uid",
    last_vmi_uid: str = "vmi-uid",
    vm_credential_id: str = RUNNER_CREDENTIAL_ID,
    vm_vmi_uid: str = "vmi-uid",
    vmi_credential_id: str = RUNNER_CREDENTIAL_ID,
    sandbox_uid: str = "sb-uid",
) -> dict[str, dict]:
    vmi_spec: dict = {"domain": {"devices": {}}}
    if vmi_volumes is not None:
        vmi_spec["volumes"] = vmi_volumes
    if vmi_filesystems is not None:
        vmi_spec["domain"]["devices"]["filesystems"] = vmi_filesystems
    return {
        "vmi": {
            "metadata": {
                "uid": "vmi-uid",
                "labels": {RUNNER_CREDENTIAL_LABEL: vmi_credential_id},
                "ownerReferences": [owner("kubevirt.io/v1", "VirtualMachine", vm_uid)],
            },
            "spec": vmi_spec,
        },
        "vm": {
            "metadata": {
                "uid": "vm-uid",
                "annotations": {
                    RUNNER_CREDENTIAL_ID_ANNOTATION: vm_credential_id,
                    RUNNER_CREDENTIAL_VMI_UID_ANNOTATION: vm_vmi_uid,
                },
                "ownerReferences": [owner(f"{GROUP}/{VERSION}", vm_owner_kind, sandbox_uid)],
            }
        },
        "sandbox": {
            "metadata": {"uid": sandbox_uid, "name": "sandbox-a"},
            "status": {"runnerCredential": {"kind": credential_kind, "lastVmiUid": last_vmi_uid}},
        },
        "credential": {
            "metadata": {
                "name": "sandbox-a",
                "ownerReferences": [owner(f"{GROUP}/{VERSION}", "Sandbox", record_owner_uid)],
            },
            "spec": {
                "subject": {"identity": {"name": "agent-a", "uid": "agent-uid"}},
                "binding": {"vmiUID": binding_vmi_uid},
            },
            "status": {
                "credentialID": record_credential_id,
                "secretName": f"svid-sandbox-a-{record_credential_id}",
                "acceptedBinding": {"vmiUID": accepted_vmi_uid},
            },
        },
    }


def launcher() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "virt-launcher-sandbox-a",
            "namespace": NAMESPACE,
            "labels": dict(LAUNCHER_LABELS),
            "ownerReferences": [owner("kubevirt.io/v1", "VirtualMachineInstance", "vmi-uid")],
        },
        "spec": {"containers": [{"name": "compute", "image": "launcher:test"}]},
    }


@dataclass(frozen=True)
class Admission:
    """What the two webhooks decide for one Pod: the mutation, then validation of the mutated Pod."""

    mutation: KyvernoApplyResult
    validation: KyvernoApplyResult | None

    @property
    def ok(self) -> bool:
        return self.mutation.ok and self.validation is not None and self.validation.ok

    @property
    def stdout(self) -> str:
        return self.mutation.stdout + (self.validation.stdout if self.validation else "")

    @property
    def mutated_resources(self) -> list[dict]:
        return self.mutation.mutated_resources


def mutation_policy(policy: Policies, **supplied: dict) -> dict:
    mutation = deepcopy(policy.mutation)
    supplied = {**supplied, "templatePod": RUNNER_POD["spec"]}
    for variable in mutation["spec"]["variables"]:
        if variable["name"] in supplied:
            variable["expression"] = f"json.unmarshal({json.dumps(json.dumps(supplied[variable['name']]))})"
    return mutation


def validation_policy(policy: Policies, operation: str, **supplied: dict) -> dict:
    validation = deepcopy(policy.validation)
    for variable in validation["spec"]["variables"]:
        if variable["name"] in supplied:
            # A CEL literal of mixed types does not type-check, so the object is parsed from JSON text.
            variable["expression"] = f"json.unmarshal({json.dumps(json.dumps(supplied[variable['name']]))})"
    for check in validation["spec"]["validations"]:
        check["expression"] = check["expression"].replace("request.operation", f"'{operation}'")
    return validation


def apply(
    tmp_path: Path,
    policy: Policies,
    resource: dict,
    *,
    username: str = CONTROLLER_USERNAME,
    operation: str = "CREATE",
    mutation: dict | None = None,
    validation: dict | None = None,
    vm_uid: str = "vm-uid",
    vm_owner_kind: str = "Sandbox",
    vmi_volumes: tuple[dict, ...] | None = (),
    vmi_filesystems: tuple[dict, ...] | None = (),
    credential_kind: str = "VM",
    record_credential_id: str = RUNNER_CREDENTIAL_ID,
    record_owner_uid: str = "sb-uid",
    binding_vmi_uid: str = "vmi-uid",
    accepted_vmi_uid: str = "vmi-uid",
    last_vmi_uid: str = "vmi-uid",
    vm_credential_id: str = RUNNER_CREDENTIAL_ID,
    vm_vmi_uid: str = "vmi-uid",
    vmi_credential_id: str = RUNNER_CREDENTIAL_ID,
    sandbox_uid: str = "sb-uid",
) -> Admission:
    """Mutation rules match only launcher creates, so an UPDATE is validated as it stands."""
    supplied = reads(
        vm_uid=vm_uid,
        vm_owner_kind=vm_owner_kind,
        vmi_volumes=list(vmi_volumes) if vmi_volumes is not None else None,
        vmi_filesystems=list(vmi_filesystems) if vmi_filesystems is not None else None,
        credential_kind=credential_kind,
        record_credential_id=record_credential_id,
        record_owner_uid=record_owner_uid,
        binding_vmi_uid=binding_vmi_uid,
        accepted_vmi_uid=accepted_vmi_uid,
        last_vmi_uid=last_vmi_uid,
        vm_credential_id=vm_credential_id,
        vm_vmi_uid=vm_vmi_uid,
        vmi_credential_id=vmi_credential_id,
        sandbox_uid=sandbox_uid,
    )
    pod = tmp_path / "pod.yaml"
    pod.write_text(yaml.safe_dump(resource))
    mutated: KyvernoApplyResult | None = None
    if operation == "CREATE":
        mutation_path = tmp_path / "mutation.yaml"
        mutation_path.write_text(
            yaml.safe_dump(mutation if mutation is not None else mutation_policy(policy, **supplied))
        )
        mutated = apply_policy(mutation_path, pod)
        if not mutated.ok:
            return Admission(mutation=mutated, validation=None)
        resource = one(r for r in mutated.mutated_resources if r["kind"] == "Pod")
        pod.write_text(yaml.safe_dump(resource))
    validation_path = tmp_path / "validation.yaml"
    validation_path.write_text(
        yaml.safe_dump(validation if validation is not None else validation_policy(policy, operation, **supplied))
    )
    userinfo = tmp_path / "userinfo.yaml"
    userinfo.write_text(yaml.safe_dump({"userInfo": {"username": username}}))
    return Admission(
        mutation=mutated or KyvernoApplyResult(passed=0, failed=0, errored=0, stdout=""),
        validation=apply_policy(validation_path, pod, userinfo_path=userinfo),
    )


def test_injects_relay_idempotently(tmp_path: Path, policy: Policies) -> None:
    first = apply(tmp_path, policy, launcher())
    assert first.ok, first.stdout
    mutated = one(r for r in first.mutated_resources if r["kind"] == "Pod")
    # Kyverno runs again when a later webhook mutates the Pod; the second pass must change nothing.
    second = apply(tmp_path, policy, deepcopy(mutated))
    assert second.ok, second.stdout
    assert second.mutated_resources == first.mutated_resources

    spec = mutated["spec"]
    assert spec["serviceAccountName"] == "default"
    assert spec["automountServiceAccountToken"] is False
    assert spec["imagePullSecrets"] == [{"name": "relay-test-pull"}]
    assert [v["name"] for v in spec["volumes"]] == [SIDECAR_SETTINGS_VOLUME_NAME, RUNNER_TLS_VOLUME_NAME]
    assert not any(c["name"] == "egress-sidecar" for c in spec["containers"])
    relay = one(c for c in spec["initContainers"] if c["name"] == "egress-sidecar")
    assert relay["restartPolicy"] == "Always"
    assert relay["image"] == "sidecar:test"
    assert {mount["name"] for mount in relay["volumeMounts"]} == {SIDECAR_SETTINGS_VOLUME_NAME, RUNNER_TLS_VOLUME_NAME}
    # The runner's settings listen on loopback; the guest dials the relay from outside it.
    assert one(v for v in spec["volumes"] if v["name"] == SIDECAR_SETTINGS_VOLUME_NAME)["configMap"] == {
        "name": RELAY_SETTINGS
    }
    assert relay["securityContext"]["runAsNonRoot"] is True
    compute = one(c for c in spec["containers"] if c["name"] == "compute")
    assert not compute.get("volumeMounts")

    update = apply(tmp_path, policy, deepcopy(mutated), operation="UPDATE")
    assert update.ok, update.stdout

    legacy = deepcopy(mutated)
    legacy_relay = one(c for c in legacy["spec"]["initContainers"] if c["name"] == "egress-sidecar")
    legacy_relay.pop("restartPolicy")
    legacy["spec"]["initContainers"].remove(legacy_relay)
    legacy["spec"]["containers"].append(legacy_relay)
    legacy_update = apply(tmp_path, policy, legacy, operation="UPDATE")
    assert legacy_update.ok, legacy_update.stdout


def test_rejects_a_missing_credential_during_mutation_even_if_validation_reads_it_later(
    tmp_path: Path, policy: Policies
) -> None:
    supplied = reads(vm_uid="vm-uid", vm_owner_kind="Sandbox", vmi_volumes=[], vmi_filesystems=[])
    supplied["credential"] = {"metadata": {"name": "sandbox-a"}, "spec": {}, "status": {}}
    # The mutation and validation webhooks read independently. A later issued
    # record must not admit a Pod mutated with a nonexistent Secret name.
    result = apply(tmp_path, policy, launcher(), mutation=mutation_policy(policy, **supplied))
    assert not result.ok, result.stdout


def test_mtls_relay_gets_status_bound_secret_and_workload_does_not(tmp_path: Path, policy: Policies) -> None:
    first = apply(tmp_path, policy, launcher())
    assert first.ok, first.stdout
    mutated = one(r for r in first.mutated_resources if r["kind"] == "Pod")
    spec = mutated["spec"]
    tls_volume = one(v for v in spec["volumes"] if v["name"] == RUNNER_TLS_VOLUME_NAME)
    assert tls_volume["projected"]["defaultMode"] == 292
    assert tls_volume["projected"]["sources"][0]["secret"]["name"] == RUNNER_SECRET
    # The workload CA alone, at the file the relay's proxy transport verifies servers with.
    assert tls_volume["projected"]["sources"][1]["configMap"] == {
        "name": "agentplane-workload-ca-bundle",
        "items": [{"key": "ca-certificates.crt", "path": sidecar_config.RUNNER_WORKLOAD_TRUST_FILE.name}],
    }
    relay = one(c for c in spec["initContainers"] if c["name"] == "egress-sidecar")
    assert {
        "name": RUNNER_TLS_VOLUME_NAME,
        "mountPath": "/run/agentplane/environment-runtime-tls",
        "readOnly": True,
    } in relay["volumeMounts"]
    assert not any(
        mount["name"] == RUNNER_TLS_VOLUME_NAME for c in spec["containers"] for mount in c.get("volumeMounts", [])
    )
    assert not any(
        mount["name"] == RUNNER_TLS_VOLUME_NAME
        for c in spec["initContainers"]
        if c is not relay
        for mount in c.get("volumeMounts", [])
    )

    workload_mount = deepcopy(mutated)
    workload_mount["spec"]["containers"][0].setdefault("volumeMounts", []).append(
        {"name": RUNNER_TLS_VOLUME_NAME, "mountPath": "/runner-tls", "readOnly": True}
    )
    update = apply(tmp_path, policy, workload_mount, operation="UPDATE")
    assert not update.ok

    create_with_workload_mount = launcher()
    create_with_workload_mount["spec"]["containers"][0]["volumeMounts"] = [
        {"name": RUNNER_TLS_VOLUME_NAME, "mountPath": "/runner-tls", "readOnly": True}
    ]
    create = apply(tmp_path, policy, create_with_workload_mount)
    assert not create.ok


@pytest.mark.parametrize("with_existing_init", [False, True])
def test_keeps_filesystem_daemons_until_compute_exits(
    tmp_path: Path, policy: Policies, with_existing_init: bool
) -> None:
    resource = launcher()
    daemons = [
        {
            "name": f"virtiofs-{volume}",
            "image": "launcher:test",
            "command": ["/usr/libexec/virtiofsd"],
            "args": [f"--socket-path=/sockets/{volume}.sock", f"--shared-dir=/{volume}", "--sandbox=none"],
            "volumeMounts": [{"name": volume, "mountPath": f"/{volume}"}],
            "resources": {"requests": {"memory": "1M"}, "limits": {"memory": "80M"}},
            "securityContext": {"runAsUser": 107, "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}},
        }
        for volume in ["state", "workspace", "runner-tls", "runner-trust"]
    ]
    unrelated = {"name": "virtiofs-unrelated", "image": "other:test", "command": ["/bin/other"]}
    container_path = {
        "name": "virtiofs-config",
        "image": "launcher:test",
        "command": ["/usr/libexec/virtiofsd"],
        "args": ["--shared-dir=/config"],
    }
    resource["spec"]["containers"][0]["readinessProbe"] = {"exec": {"command": ["/usr/bin/virt-probe"]}}
    original_compute = deepcopy(resource["spec"]["containers"][0])
    existing_init = (
        [
            {"name": name, "image": "launcher:test", "command": ["/bin/copy"]}
            for name in ["container-disk-copy", "prepare-paths"]
        ]
        if with_existing_init
        else []
    )
    resource["spec"]["containers"].extend([*daemons[:2], unrelated, container_path, *daemons[2:]])
    if with_existing_init:
        resource["spec"]["initContainers"] = existing_init
    volumes = (
        {"name": "state", "persistentVolumeClaim": {"claimName": "state"}},
        {"name": "workspace", "persistentVolumeClaim": {"claimName": "workspace"}},
        {"name": "runner-tls", "secret": {"secretName": "runner-tls"}},
        {"name": "runner-trust", "configMap": {"name": "runner-trust"}},
        {"name": "config", "containerPath": {"path": "/config"}},
    )

    filesystems = tuple({"name": volume["name"], "virtiofs": {}} for volume in volumes)
    first = apply(tmp_path, policy, resource, vmi_volumes=volumes, vmi_filesystems=filesystems)
    assert first.ok, first.stdout
    mutated = one(first.mutated_resources)
    spec = mutated["spec"]
    assert one(c for c in spec["containers"] if c["name"] == "compute") == original_compute
    assert one(c for c in spec["containers"] if c["name"] == unrelated["name"]) == unrelated
    assert one(c for c in spec["containers"] if c["name"] == container_path["name"]) == container_path
    assert not any(c["name"] in {d["name"] for d in daemons} for c in spec["containers"])
    assert spec["initContainers"][: len(existing_init)] == existing_init
    relay = one(c for c in spec["initContainers"] if c["name"] == "egress-sidecar")
    assert relay["restartPolicy"] == "Always"
    assert {c["name"]: c for c in spec["initContainers"][len(existing_init) :] if c["name"] != relay["name"]} == {
        daemon["name"]: {**daemon, "restartPolicy": "Always"} for daemon in daemons
    }

    second = apply(tmp_path, policy, deepcopy(mutated), vmi_volumes=volumes, vmi_filesystems=filesystems)
    assert second.ok, second.stdout
    assert second.mutated_resources == first.mutated_resources


_STATE_VOLUME = {"name": "state", "persistentVolumeClaim": {"claimName": "state"}}
_STATE_FILESYSTEM = {"name": "state", "virtiofs": {}}
_STATE_DAEMON = {"name": "virtiofs-state", "image": "launcher:test", "command": ["/usr/libexec/virtiofsd"]}


@pytest.mark.parametrize(
    "defect", ["ordinary-wrong-command", "native-wrong-command", "missing", "duplicate", "non-restartable"]
)
def test_fails_closed_for_eligible_daemon(tmp_path: Path, policy: Policies, defect: str) -> None:
    resource = launcher()
    daemon = deepcopy(_STATE_DAEMON)
    if defect == "ordinary-wrong-command":
        daemon["command"] = ["/bin/false"]
        resource["spec"]["containers"].append(daemon)
    elif defect == "native-wrong-command":
        daemon["command"] = ["/bin/false"]
        daemon["restartPolicy"] = "Always"
        resource["spec"]["initContainers"] = [daemon]
    elif defect == "duplicate":
        daemon["restartPolicy"] = "Always"
        resource["spec"]["initContainers"] = [daemon]
        # The ordinary copy is deliberately malformed so the mutation cannot
        # move it; post-mutation validation must reject both classes occupied.
        resource["spec"]["containers"].append({**daemon, "command": ["/bin/false"]})
    elif defect == "non-restartable":
        resource["spec"]["initContainers"] = [daemon]
    result = apply(tmp_path, policy, resource, vmi_volumes=(_STATE_VOLUME,), vmi_filesystems=(_STATE_FILESYSTEM,))
    assert not result.ok, result.stdout

    # With just the new guard removed, the other real Kyverno rules admit this
    # launcher's unchanged daemon layout; the guard is what closes this gap.
    without_guard = validation_policy(
        policy,
        "CREATE",
        **reads(
            vm_uid="vm-uid", vm_owner_kind="Sandbox", vmi_volumes=[_STATE_VOLUME], vmi_filesystems=[_STATE_FILESYSTEM]
        ),
    )
    without_guard["spec"]["validations"] = [
        check for check in without_guard["spec"]["validations"] if "eligibleVirtiofs" not in check["expression"]
    ]
    prior = apply(
        tmp_path,
        policy,
        resource,
        validation=without_guard,
        vmi_volumes=(_STATE_VOLUME,),
        vmi_filesystems=(_STATE_FILESYSTEM,),
    )
    assert prior.ok, prior.stdout


def test_rejects_missing_second_eligible_daemon(tmp_path: Path, policy: Policies) -> None:
    resource = launcher()
    resource["spec"]["containers"].append(deepcopy(_STATE_DAEMON))
    result = apply(
        tmp_path,
        policy,
        resource,
        vmi_volumes=(_STATE_VOLUME, {"name": "trust", "configMap": {"name": "trust"}}),
        vmi_filesystems=(_STATE_FILESYSTEM, {"name": "trust", "virtiofs": {}}),
    )
    assert not result.ok, result.stdout


@pytest.mark.parametrize(
    ("volume", "filesystem"),
    [
        ({"name": "config", "containerPath": {"path": "/config"}}, {"name": "config", "virtiofs": {}}),
        ({"name": "config", "persistentVolumeClaim": {"claimName": "disk-only"}}, None),
        ({"name": "config", "secret": {"secretName": "disk-only"}}, None),
        ({"name": "config", "configMap": {"name": "disk-only"}}, None),
    ],
)
def test_leaves_noneligible_daemon_ordinary(
    tmp_path: Path, policy: Policies, volume: dict, filesystem: dict | None
) -> None:
    resource = launcher()
    daemon = {**_STATE_DAEMON, "name": "virtiofs-config"}
    resource["spec"]["containers"].append(daemon)
    result = apply(
        tmp_path, policy, resource, vmi_volumes=(volume,), vmi_filesystems=(filesystem,) if filesystem else ()
    )
    assert result.ok, result.stdout
    mutated = one(result.mutated_resources)
    assert one(c for c in mutated["spec"]["containers"] if c["name"] == daemon["name"]) == daemon
    assert all(c["name"] != daemon["name"] for c in mutated["spec"]["initContainers"])


@pytest.mark.parametrize(
    ("volumes", "filesystems"),
    [
        pytest.param((_STATE_VOLUME,), None, id="backed-volume-absent-filesystems"),
        pytest.param((_STATE_VOLUME,), (), id="backed-volume-empty-filesystems"),
        pytest.param(None, (_STATE_FILESYSTEM,), id="absent-volumes-filesystem"),
        pytest.param((), (_STATE_FILESYSTEM,), id="empty-volumes-filesystem"),
        pytest.param(None, None, id="both-fields-absent"),
        pytest.param(
            ({"name": "state", "containerPath": {"path": "/state"}},), (_STATE_FILESYSTEM,), id="container-path-only"
        ),
    ],
)
def test_omitted_vmi_arrays_have_no_eligible_daemon(
    tmp_path: Path, policy: Policies, volumes: tuple[dict, ...] | None, filesystems: tuple[dict, ...] | None
) -> None:
    resource = launcher()
    resource["spec"]["containers"].append(deepcopy(_STATE_DAEMON))
    result = apply(tmp_path, policy, resource, vmi_volumes=volumes, vmi_filesystems=filesystems)
    assert result.ok, result.stdout
    mutated = one(result.mutated_resources)
    assert one(c for c in mutated["spec"]["containers"] if c["name"] == _STATE_DAEMON["name"]) == _STATE_DAEMON
    assert all(c["name"] != _STATE_DAEMON["name"] for c in mutated["spec"]["initContainers"])


@pytest.mark.parametrize(
    "forgery",
    [
        pytest.param({"username": "system:serviceaccount:attacker:creator"}, id="not-kubevirt"),
        pytest.param({"vm_uid": "replaced-vm"}, id="stale-vm"),
        pytest.param({"vm_owner_kind": "NotASandbox"}, id="vm-not-a-sandbox"),
        pytest.param(
            {"vmi_volumes": ({"name": "sa", "serviceAccount": {"serviceAccountName": "x"}},)}, id="guest-token"
        ),
    ],
)
def test_rejects_unproven_launcher(tmp_path: Path, policy: Policies, forgery: dict) -> None:
    assert not apply(tmp_path, policy, launcher(), **forgery).ok


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param({"credential_kind": "Pod"}, id="not-a-vm-binding"),
        pytest.param({"record_credential_id": "fedcba9876543210fedcba9876543210"}, id="record-id-mismatch"),
        pytest.param({"record_owner_uid": "replacement-sandbox"}, id="record-of-another-sandbox"),
        pytest.param({"binding_vmi_uid": "stale-vmi"}, id="record-bound-to-another-vmi"),
        pytest.param({"accepted_vmi_uid": "stale-vmi"}, id="record-accepted-another-vmi"),
        pytest.param({"last_vmi_uid": "stale-vmi"}, id="status-vmi-mismatch"),
        pytest.param({"vm_vmi_uid": "stale-vmi"}, id="vm-annotation-vmi-mismatch"),
        pytest.param({"vmi_credential_id": "fedcba9876543210fedcba9876543210"}, id="vmi-label-mismatch"),
        pytest.param({"sandbox_uid": "replacement-sandbox"}, id="sandbox-owner-mismatch"),
    ],
)
def test_mtls_rejects_launcher_without_current_credential_binding(
    tmp_path: Path, policy: Policies, binding: dict
) -> None:
    assert not apply(tmp_path, policy, launcher(), **binding).ok


def test_rejects_stale_vmi(tmp_path: Path, policy: Policies) -> None:
    resource = launcher()
    resource["metadata"]["ownerReferences"][0]["uid"] = "replaced-vmi"
    assert not apply(tmp_path, policy, resource).ok


def test_rejects_relay_in_both_container_classes(tmp_path: Path, policy: Policies) -> None:
    resource = launcher()
    resource["spec"]["containers"].append(
        {
            "name": "egress-sidecar",
            "image": "sidecar:test",
            "volumeMounts": [{"name": RUNNER_TLS_VOLUME_NAME, "mountPath": "/run/agentplane/workload-svid"}],
        }
    )
    assert not apply(tmp_path, policy, resource).ok


@pytest.mark.parametrize(
    ("field", "operation"), [("containers", "CREATE"), ("initContainers", "CREATE"), ("ephemeralContainers", "UPDATE")]
)
def test_rejects_runner_credential_mount_outside_relay(
    tmp_path: Path, policy: Policies, field: str, operation: str
) -> None:
    resource = launcher()
    resource["spec"].setdefault(field, []).append(
        {
            "name": "intruder",
            "image": "probe:test",
            "volumeMounts": [{"name": RUNNER_TLS_VOLUME_NAME, "mountPath": "/t"}],
        }
    )
    assert not apply(tmp_path, policy, resource, operation=operation).ok


if __name__ == "__main__":
    pytest_bazel.main()
