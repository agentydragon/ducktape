"""Fresh testing-only storage fixtures; deterministic file probes, without model turns."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest
import pytest_bazel
import yaml
from kubernetes_asyncio import client as k8s, config as k8s_config

from agentplane.app.testing.client import Client
from agentplane.crd_group import GROUP, VERSION
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from util.testing.undeclared_outputs import undeclared_outputs_dir

NAMESPACE = "agentplane-testing"
BOUND_SECONDS = 300
RECORD = "agentplane.allegedly.works/workspace-record"


async def _kubectl(*args: str, body: dict | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "-n",
        NAMESPACE,
        *args,
        stdin=asyncio.subprocess.PIPE if body is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(BOUND_SECONDS + 10):
            stdout, stderr = await process.communicate(json.dumps(body).encode() if body is not None else None)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {stderr.decode()}")
    return stdout.decode().strip()


async def _object(kind: str, name: str) -> dict:
    decoded = json.loads(await _kubectl("get", kind, name, "-o", "json"))
    assert isinstance(decoded, dict)
    return decoded


async def _condition(name: str, condition: str, value: str = "True") -> None:
    await _kubectl(
        "wait",
        f"sandboxes.{GROUP}/{name}",
        f'--for=jsonpath={{.status.conditions[?(@.type=="{condition}")].status}}={value}',
        f"--timeout={BOUND_SECONDS}s",
    )


async def _running(name: str, uid: str, value: bool) -> None:
    await _kubectl(
        "patch",
        f"sandboxes.{GROUP}",
        name,
        "--type=merge",
        "-p",
        json.dumps({"metadata": {"uid": uid}, "spec": {"running": value}}),
    )


async def _delete(plural: str, name: str, uid: str) -> None:
    configuration = k8s.Configuration()
    await k8s_config.load_kube_config(client_configuration=configuration)
    async with k8s.ApiClient(configuration) as api:
        await k8s.CustomObjectsApi(api).delete_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            plural,
            name,
            body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid)),
        )
    await _kubectl("wait", f"{plural}.{GROUP}/{name}", "--for=delete", "--timeout=120s")


async def _suspend(name: str, uid: str) -> None:
    await _running(name, uid, False)
    await _kubectl(
        "wait",
        f"sandboxes.{GROUP}/{name}",
        '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=Stopped',
        f"--timeout={BOUND_SECONDS}s",
    )


async def _cleanup(name: str, uid: str, claims: list[str]) -> None:
    errors: list[Exception] = []
    try:
        await _suspend(name, uid)
    except Exception as error:
        errors.append(error)
    # Delete remains allowed when bootstrap/startup cannot suspend. It follows the controller's
    # ordinary seal policy; the fixture never removes a finalizer or forces shared state.
    try:
        await _delete("sandboxes", name, uid)
        for claim in claims:
            await _kubectl("wait", f"pvc/{claim}", "--for=delete", "--timeout=120s")
    except Exception as error:
        errors.append(error)
    if errors:
        raise ExceptionGroup("storage fixture cleanup failed", errors)


async def _volumes(name: str, uid: str, *, retained: bool) -> dict:
    state = await _object("pvc", f"{name}-state")
    workspace = await _object("pvc", f"{name}-workspace")
    assert state["status"]["phase"] == workspace["status"]["phase"] == "Bound"
    assert any(owner["uid"] == uid and owner.get("controller") for owner in state["metadata"]["ownerReferences"])
    record = workspace["metadata"].get("annotations", {}).get(RECORD)
    if retained:
        assert any(
            owner["uid"] == uid and owner.get("controller") for owner in workspace["metadata"]["ownerReferences"]
        )
        assert record is not None
        binding = (await _object(f"sandboxes.{GROUP}", name))["status"]["volumes"]["workspace"]
        assert binding["name"] == workspace["metadata"]["name"]
        assert binding["uid"] == workspace["metadata"]["uid"]
        assert binding["layout"] == json.loads(record)["layout"]
    else:
        assert record is None
        assert all(owner["uid"] != uid for owner in workspace["metadata"]["ownerReferences"])
    return {
        "state": {
            "name": state["metadata"]["name"],
            "uid": state["metadata"]["uid"],
            "pv": state["spec"]["volumeName"],
        },
        "workspace": {
            "name": workspace["metadata"]["name"],
            "uid": workspace["metadata"]["uid"],
            "pv": workspace["spec"]["volumeName"],
            "record": record,
        },
    }


async def _probe(
    client: Client, name: str, cwd: str, workspace_dir: str, marker: str, *, workspace_exists: bool
) -> dict:
    state = "/state/storage-acceptance-state"
    workspace = f"{workspace_dir}/storage-acceptance-workspace"
    source = f'set -eu\ntest "$(cat {state})" = {shlex.quote(marker)}\nsha256sum {state}\n'
    if workspace_exists:
        source += f'test "$(cat {workspace})" = {shlex.quote(marker)}\nsha256sum {workspace}\n'
    else:
        source += f"test ! -e {workspace}\nprintf 'workspace=absent\\n'\n"
    if workspace_dir == "/workspace":
        # The Pod image declares awk/coreutils, but not util-linux's findmnt.
        source += "awk '$5 == \"/workspace\" { print; found=1 } END { exit !found }' /proc/self/mountinfo\n"
    else:
        source += f"findmnt -n -o FSTYPE,SOURCE,TARGET -T {workspace_dir}\n"
    source += "printf 'STORAGE_PROBE_OK\\n'\n"
    session = uuid4()
    await client.create_session(
        name,
        session,
        overrides={
            "harness": "HARNESS_CODEX",
            "model": "chatgpt/oai-responses/gpt-6-luna",
            "cwd": cwd,
            "reasoningEffort": "low",
            "instructions": "",
        },
        setup_script=source,
    )
    stdout, stderr = bytearray(), bytearray()
    async with asyncio.timeout(BOUND_SECONDS):
        async for entry in client.events(session, after=0, read_seconds=BOUND_SECONDS):
            if entry.event.HasField("setup_output"):
                stdout.extend(entry.event.setup_output.stdout)
                stderr.extend(entry.event.setup_output.stderr)
            if entry.event.HasField("setup_finished"):
                assert entry.event.setup_finished.exit_code == 0, stderr.decode()
                assert "STORAGE_PROBE_OK" in stdout.decode()
                return {"session": str(session), "stdout": stdout.decode(), "setup_exit_code": 0}
            assert not entry.event.HasField("setup_interrupted"), "storage probe setup was interrupted"
    raise AssertionError("storage probe ended without its setup result")


@pytest.mark.parametrize("case", ["pod-directory", "vm-raw", "vm-legacy"])
async def test_fresh_workspace_storage_survives_replacement(client: Client, base_url: str, case: str) -> None:
    # This test cannot be redirected to staging via the standard client fixture.
    assert base_url.rstrip("/") == "https://agentplane-testing.allegedly.works"
    controller = await _object("deployment", "agentplane-sandbox-controller")
    assert controller["status"].get("readyReplicas") == 1
    # The operator supplies the exact source-verified accepting pin before the run. A concurrent
    # image rollout fails this gate before any new-layout template is written.
    controller_image = controller["spec"]["template"]["spec"]["containers"][0]["image"]
    assert controller_image == os.environ["AGENTPLANE_STORAGE_CONTROLLER_IMAGE"], controller_image
    existing = json.loads(await _kubectl("get", f"sandboxes.{GROUP}", "-o", "json"))["items"]
    assert not existing, "serialize this acceptance with other testing Sandbox users"
    quota = json.loads(await _kubectl("get", "resourcequota", "-o", "json"))["items"]
    assert quota[0]["status"]["used"]["requests.storage"] == "5Gi", "unexpected active testing storage"
    vm, retained = case.startswith("vm"), case != "vm-legacy"
    base = await _object(f"sandboxtemplates.{GROUP}", "runner-vm" if vm else "runner")
    name = f"accept-storage-{case}-{uuid4().hex[:8]}"
    template = name
    spec = copy.deepcopy(base["spec"])
    workspace_dir = "/state/workspaces" if vm else "/workspace"
    # The guest creates .home before starting its runner, including on a new legacy disk. The
    # runner requires a child of its workspace root; the mount root itself is forbidden.
    cwd = f"{workspace_dir}/.home" if vm else workspace_dir
    if retained:
        spec["workspaceVolume"] = {
            "name": "workspace",
            "claim": {**copy.deepcopy(spec["stateVolume"]["claim"]), "volumeMode": "Filesystem"},
            "layout": {"type": "RawDiskImage", "filesystem": "ext4"} if vm else {"type": "DirectoryTree"},
        }
        if vm:
            spec["vm"].pop("dataVolumeTemplates")
            spec["vm"]["template"]["spec"]["volumes"] = [
                volume for volume in spec["vm"]["template"]["spec"]["volumes"] if volume["name"] != "workspace"
            ]
        else:
            runner = next(container for container in spec["pod"]["spec"]["containers"] if container["name"] == "runner")
            runner["volumeMounts"].append({"name": "workspace", "mountPath": workspace_dir})
    marker = f"storage-test-{uuid4().hex}"
    bootstrap = f"set -eu\nprintf '%s\\n' {marker} > /state/storage-acceptance-state\nprintf '%s\\n' {marker} > {workspace_dir}/storage-acceptance-workspace\nsync\n"
    config = await _object("configmap", "agentplane-sandbox-service-config")
    policies = yaml.safe_load(config["data"]["config.yaml"])["default_policy_sets"]
    assert policies
    assert all(isinstance(policy, str) for policy in policies)
    evidence: dict = {
        "case": case,
        "namespace": NAMESPACE,
        "controller_image": controller_image,
        "base_template": base,
        "sandbox": name,
        "passed": False,
        "cleanup_complete": False,
    }
    path = undeclared_outputs_dir() / f"storage-{case}.json"

    def save() -> None:
        path.write_text(json.dumps(evidence, indent=2) + "\n")

    save()
    async with AsyncExitStack() as stack:
        created_template = json.loads(
            await _kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": "SandboxTemplate",
                    "metadata": {"name": template, "namespace": NAMESPACE},
                    "spec": spec,
                },
            )
        )
        stack.push_async_callback(_delete, "sandboxtemplates", template, created_template["metadata"]["uid"])
        created = json.loads(
            await _kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": "Sandbox",
                    "metadata": {"name": name, "namespace": NAMESPACE},
                    "spec": {"template": template, "running": True, "policySets": policies, "bootstrap": bootstrap},
                },
            )
        )
        uid = created["metadata"]["uid"]
        evidence["sandbox_uid"] = uid
        stack.push_async_callback(_cleanup, name, uid, [f"{name}-state", f"{name}-workspace"])
        save()
        await _condition(name, "Ready")
        await _condition(name, "Bootstrapped")
        old_incarnation = (await _object(f"sandboxes.{GROUP}", name))["status"]["incarnation"]
        before = await _volumes(name, uid, retained=retained)
        workload = await _object("virtualmachine" if vm else "pod", name)
        workload_spec = workload["spec"]["template"]["spec"] if vm else workload["spec"]
        attachment = next(volume for volume in workload_spec["volumes"] if volume["name"] == "workspace")
        if retained:
            assert attachment["persistentVolumeClaim"]["claimName"] == before["workspace"]["name"]
        else:
            assert attachment["dataVolume"]["name"] == before["workspace"]["name"]
        evidence.update(
            before=before,
            old_incarnation=old_incarnation,
            workspace_attachment=attachment,
            first_probe=await _probe(client, name, cwd, workspace_dir, marker, workspace_exists=True),
        )
        if vm:
            assert "\next4 " in evidence["first_probe"]["stdout"]
        save()
        await _suspend(name, uid)
        await _kubectl("wait", f"pod/{old_incarnation['name']}", "--for=delete", "--timeout=120s")
        if vm:
            await _kubectl("wait", f"virtualmachine/{name}", "--for=delete", "--timeout=120s")
        if retained:
            assert await _volumes(name, uid, retained=True) == before
            # Exercise the durable record at the actual resume boundary: an incompatible claim
            # edit must refuse before starting any replacement or changing retained contents.
            changed = await _object(f"sandboxtemplates.{GROUP}", template)
            changed["spec"]["workspaceVolume"]["claim"]["resources"]["requests"]["storage"] = "11Gi"
            await _kubectl("replace", "-f", "-", body=changed)
            await _running(name, uid, True)
            await _kubectl(
                "wait",
                f"sandboxes.{GROUP}/{name}",
                '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=VolumeLayoutMismatch',
                f"--timeout={BOUND_SECONDS}s",
            )
            refused = await _object(f"sandboxes.{GROUP}", name)
            assert not refused["status"].get("incarnation")
            assert await _volumes(name, uid, retained=True) == before
            evidence["incompatible_claim_refused"] = True
            save()
            await _suspend(name, uid)
            changed = await _object(f"sandboxtemplates.{GROUP}", template)
            changed["spec"] = spec
            await _kubectl("replace", "-f", "-", body=changed)
        else:
            await _kubectl("wait", f"pvc/{name}-workspace", "--for=delete", "--timeout=120s")
        await _running(name, uid, True)
        await _condition(name, "Ready")
        await _condition(name, "Bootstrapped")
        new_incarnation = (await _object(f"sandboxes.{GROUP}", name))["status"]["incarnation"]
        assert new_incarnation["uid"] != old_incarnation["uid"]
        after = await _volumes(name, uid, retained=retained)
        assert after["state"] == before["state"]
        if retained:
            assert after == before
        else:
            assert after["workspace"]["uid"] != before["workspace"]["uid"]
        evidence.update(
            after=after,
            new_incarnation=new_incarnation,
            resumed_probe=await _probe(client, name, cwd, workspace_dir, marker, workspace_exists=retained),
            passed=True,
        )
        save()
    evidence["cleanup_complete"] = True
    save()


if __name__ == "__main__":
    pytest_bazel.main()
