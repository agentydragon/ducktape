"""Runner mTLS certificates authenticate Pod and VM sessions across incarnation changes."""

from __future__ import annotations

import base64
import json
from uuid import uuid4

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import REQUEST_SECONDS, Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.agent import WORKING_DIRECTORY, Agent, scripted_turn_with_admission_and_history
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, Sandboxes, wait_until_running
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer

_RUNNER_CREDENTIAL_LABEL = "agentplane.allegedly.works/environment-runtime-credential"
_SANDBOX_LABEL = "agentplane.allegedly.works/sandbox"


async def _identity(cluster: Cluster, pod_name: str) -> tuple[str, str]:
    pod = json.loads(await cluster.kubectl("get", "pod", pod_name, "-o", "json"))
    metadata = pod["metadata"]
    labels = metadata["labels"]
    return (metadata["uid"], labels[_RUNNER_CREDENTIAL_LABEL])


async def _ready_certificate(
    cluster: Cluster, name: str, sandbox_name: str, sandbox_uid: str, credential_id: str, namespace: str
) -> None:
    with tracer.start_as_current_span("system_test.pki.runner_certificate"):
        certificate = json.loads(await cluster.kubectl("get", "certificate", name, "-o", "json"))
        spec = certificate["spec"]
        assert spec["secretName"] == f"runner-{credential_id}"
        assert spec["uris"] == [f"spiffe://agentplane/environment-runtime/{namespace}/{sandbox_uid}/{credential_id}"]
        assert set(spec["usages"]) == {"digital signature", "server auth", "client auth"}
        assert any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in certificate["status"]["conditions"]
        )
        secret = json.loads(await cluster.kubectl("get", "secret", f"runner-{credential_id}", "-o", "json"))
        assert secret["metadata"]["annotations"]["cert-manager.io/certificate-name"] == name
        assert secret["metadata"]["labels"][_SANDBOX_LABEL] == sandbox_name
        assert secret["metadata"]["labels"][_RUNNER_CREDENTIAL_LABEL] == credential_id
        requests = json.loads(await cluster.kubectl("get", "certificaterequests", "-o", "json"))["items"]
        issued = [
            request
            for request in requests
            if any(owner["uid"] == certificate["metadata"]["uid"] for owner in request["metadata"]["ownerReferences"])
            and any(
                c["type"] == "Ready" and c["status"] == "True" for c in request.get("status", {}).get("conditions", [])
            )
        ]
        assert len(issued) == 1
        assert any(c["type"] == "Approved" and c["status"] == "True" for c in issued[0]["status"]["conditions"])
        assert base64.b64decode(issued[0]["status"]["certificate"]) == base64.b64decode(secret["data"]["tls.crt"])


async def _wait_suspended(client: Client, target: identity_pb2.EnvironmentTarget) -> None:
    with tracer.start_as_current_span("system_test.sandbox.suspend"):
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(REQUEST_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(AssertionError),
            reraise=True,
        ):
            with attempt:
                view = await client.get_environment(target)
                assert view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED
                assert not view.kubernetes_diagnostics.HasField("pod")


async def _open_agent(
    client: Client,
    dispatcher: SessionDispatcherClient,
    view: environment_service_pb2.EnvironmentDescriptor,
    harness: session_pb2.Harness,
    model_name: str,
    *,
    cwd: str = WORKING_DIRECTORY,
) -> Agent:
    agent = await Agent.create(
        client,
        dispatcher,
        sandbox=view.target.name,
        sandbox_uid=view.target.uid,
        harness=harness,
        model=model_name,
        cwd=cwd,
    )
    await client.wait_for_thread_feed(agent.thread_id, timeout_s=REQUEST_SECONDS)
    return agent


async def cert_manager_reissues_a_deleted_tls_secret(cluster: Cluster) -> None:
    name = f"test-reissue-{uuid4().hex[:8]}"
    await cluster.kubectl(
        "apply",
        "-f",
        "-",
        body={
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": name},
            "spec": {
                "secretName": name,
                "issuerRef": {"name": "agentplane-workload", "kind": "ClusterIssuer"},
                # A one-segment service URI: the admission policy lets the manifest applier request it.
                "uris": [f"spiffe://agentplane/{name}"],
                "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                "usages": ["digital signature", "server auth"],
            },
        },
    )
    await cluster.kubectl("wait", "--for=condition=Ready", f"certificate/{name}", f"--timeout={REQUEST_SECONDS}s")
    original = json.loads(await cluster.kubectl("get", "secret", name, "-o", "json"))
    certificate = json.loads(await cluster.kubectl("get", "certificate", name, "-o", "json"))
    revision = certificate["status"]["revision"]
    await cluster.kubectl("delete", "secret", name, "--wait=false")
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(REQUEST_SECONDS),
        wait=wait_fixed(1),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            encoded = await cluster.kubectl("get", "secret", name, "--ignore-not-found", "-o", "json")
            assert encoded
            replacement = json.loads(encoded)
            assert replacement["metadata"]["uid"] != original["metadata"]["uid"]
            certificate = json.loads(await cluster.kubectl("get", "certificate", name, "-o", "json"))
            assert certificate["status"]["revision"] > revision
            assert any(c["type"] == "Ready" and c["status"] == "True" for c in certificate["status"]["conditions"])
    assert replacement["data"]["tls.crt"] != original["data"]["tls.crt"]


async def pod_mtls_session_survives_replacement_and_keeps_history(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    view = await sandbox(f"systest-runner-pod-{uuid4().hex[:8]}", template=POD_TEMPLATE)
    assert view.kubernetes_diagnostics.HasField("pod")
    old_pod_uid, old_credential_id = await _identity(cluster, view.kubernetes_diagnostics.pod.name)
    await _ready_certificate(
        cluster,
        f"runner-{old_credential_id}",
        view.target.name,
        str(view.target.uid),
        old_credential_id,
        view.target.namespace,
    )

    agent = await _open_agent(client, dispatcher, view, harness, offered_model)
    model = scripted.model(view.kubernetes_diagnostics.pod.name, harness)
    marker = f"POD_CONTEXT_{uuid4().hex}"
    turn, history_before, _ = await scripted_turn_with_admission_and_history(
        client,
        agent,
        model=model,
        command_id=f"pod-{uuid4().hex}",
        prompt=f"Remember this invented non-secret code for our next turn: {marker}. Run a shell check.",
        shell_command="printf 'POD_MTLS_OK\\n'",
        response="POD_MTLS_OK",
    )
    assert "POD_MTLS_OK" in "\n".join(turn.tool_outputs)

    await client.suspend_sandbox(view.target)
    await _wait_suspended(client, view.target)
    await client.resume_sandbox(view.target)
    resumed = await wait_until_running(client, view.target)
    assert resumed.target.uid == view.target.uid
    assert resumed.kubernetes_diagnostics.HasField("pod")
    new_pod_uid, new_credential_id = await _identity(cluster, resumed.kubernetes_diagnostics.pod.name)
    assert new_pod_uid != old_pod_uid
    assert new_credential_id != old_credential_id
    await _ready_certificate(
        cluster,
        f"runner-{new_credential_id}",
        resumed.target.name,
        str(resumed.target.uid),
        new_credential_id,
        resumed.target.namespace,
    )

    resumed_model = scripted.model(resumed.kubernetes_diagnostics.pod.name, harness)
    turn, history_after, recall = await scripted_turn_with_admission_and_history(
        client,
        agent,
        model=resumed_model,
        command_id=f"resume-{uuid4().hex}",
        prompt="Recall the invented code from our earlier conversation. Reply with only that code.",
        history_turns=2,
        response=marker,
    )
    assert turn.resumed
    assert marker in "\n".join(recall.user_texts)
    assert marker in turn.text
    history_before_serialized = [entry.SerializeToString() for entry in history_before]
    assert [entry.SerializeToString() for entry in history_after[: len(history_before)]] == history_before_serialized
    assert len(history_after) > len(history_before)


async def check_vm_runner_certificate(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> tuple[str, str]:
    assert view.kubernetes_diagnostics.HasField("pod")
    identity = await _identity(cluster, view.kubernetes_diagnostics.pod.name)
    _, credential_id = identity
    await _ready_certificate(
        cluster, f"runner-{credential_id}", view.target.name, str(view.target.uid), credential_id, view.target.namespace
    )
    return identity


async def assert_vm_runner_identity_unchanged(cluster: Cluster, pod_name: str, identity: tuple[str, str]) -> None:
    assert await _identity(cluster, pod_name) == identity


# gazelle:include_dep @pypi//protobuf
