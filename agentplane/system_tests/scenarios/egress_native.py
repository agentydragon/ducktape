"""App-free Pod Egress scenarios for workload credentials and Kubernetes identity."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime
from typing import Any

from google.protobuf import json_format
from more_itertools import one
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.action_service.api.contracts import action_service_pb2
from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import sidecar_config
from agentplane.egress.api.admin.egress_admin_pb2 import Decision
from agentplane.egress.resources import placeholder_of
from agentplane.environments.v1 import environment_service_pb2
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.system_tests.deployment import svid_box
from agentplane.system_tests.driver.action_cli import CLI, submission
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.policy_objects import PolicyObjects
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios.egress_platform import (
    BINDING_SECONDS,
    KUBERNETES_HOST,
    UNLISTED_HOST,
    create_kubernetes_probe,
    decision_for,
    kubectl_create_binding,
)
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace

# The runner container calls the Action Service as the Sandbox; the box alone mounts its SVID.
_SANDBOX_GROUP = "sandbox"
_BOX_CONTAINER = "sandbox"
_RUNNER_CONTAINER = "environment-runtime"
_BOX_SVID_DIR = "/run/agentplane/workload-svid"
_SIDECAR_PROXY = "http://127.0.0.1:3128"
_STANDING_SECONDS = 120.0


def _owned_by(*sandboxes: environment_service_pb2.EnvironmentDescriptor) -> list[dict[str, str]]:
    """Owner references that collect an object once every one of these Sandboxes is gone."""
    return [
        {"apiVersion": SANDBOX_API_VERSION, "kind": SANDBOX_KIND, "name": view.target.name, "uid": str(view.target.uid)}
        for view in sandboxes
    ]


async def _whoami(cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor, credential: str) -> str:
    """The username the API server sees for a `kubectl auth whoami` the Sandbox sends through its proxy."""
    return await cluster.exec(
        view.kubernetes_diagnostics.pod.name,
        "kubectl",
        f"--server=https://{KUBERNETES_HOST}",
        f"--token={placeholder_of(credential)}",
        "auth",
        "whoami",
        "-o",
        "jsonpath={.status.userInfo.username}",
        container=_RUNNER_CONTAINER,
    )


async def _egress_refusal(cluster: Cluster, pod: str, url: str, *headers: str) -> str | None:
    """The proxy's refusal reason for `url` from `pod`'s workload container; None when it refused nothing."""
    response = await cluster.exec(
        pod,
        "/bin/bash",
        "-c",
        """response=$("$0" "$@")
status=$?
if [ "$status" -eq 56 ] && printf '%s\\n' "$response" | grep -Eiq '^[[:space:]]*X-Agentplane-Egress:[[:space:]]*denied;[[:space:]]*reason='; then
  printf '%s\\n' "$response"
  exit 0
fi
printf '%s\\n' "$response"
exit "$status"
""",
        "curl",
        "--silent",
        # The destination's TLS is the proxy's interception, which this test does not check.
        "--insecure",
        "--proxy",
        _SIDECAR_PROXY,
        "--output",
        "/dev/null",
        "--dump-header",
        "-",
        "--max-time",
        "15",
        *(argument for header in headers for argument in ("--header", header)),
        url,
        container="workload",
    )
    for line in response.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "x-agentplane-egress":
            return value.strip().removeprefix("denied; reason=")
    return None


def _with_secret(volume: dict[str, Any], secret: str) -> dict[str, Any]:
    """The runner's projected TLS volume with the standing record's Secret as its Secret source. Its
    ConfigMap sources stay: the sidecar verifies the proxy against `workload-ca.crt`, which no SVID
    Secret carries."""
    projected = volume["projected"]
    sources = [
        {"secret": {**source["secret"], "name": secret}} if "secret" in source else source
        for source in projected["sources"]
    ]
    return {**volume, "projected": {**projected, "sources": sources}}


def _standing_pod_spec(runner: dict[str, Any], box_labels: dict[str, str], secret: str) -> dict[str, Any]:
    """The runner Pod's own egress sidecar, presenting the standing record's Secret in place of the
    incarnation's, beside an idle workload container. Nothing else of the runner is copied."""
    spec = runner["spec"]
    sidecar = next(c for c in spec["initContainers"] if c["name"] == sidecar_config.CONTAINER_NAME)
    mounted = {mount["name"] for mount in sidecar["volumeMounts"]}
    (tls_volume,) = (
        m["name"] for m in sidecar["volumeMounts"] if m["mountPath"] == sidecar_config.RUNNER_TLS_MOUNT_PATH
    )
    volumes = [
        _with_secret(volume, secret) if volume["name"] == tls_volume else volume
        for volume in spec["volumes"]
        if volume["name"] in mounted
    ]
    runtime = spec["containers"][0]
    workload = {
        "name": "workload",
        "image": runtime["image"],
        "imagePullPolicy": "IfNotPresent",
        "command": ["bash", "-c", "trap 'exit 0' TERM; sleep infinity & wait"],
        "securityContext": runtime.get("securityContext", {}),
    }
    copied = ("serviceAccountName", "automountServiceAccountToken", "securityContext", "imagePullSecrets")
    return {
        "metadata": {"labels": box_labels},
        "spec": {
            **{key: spec[key] for key in copied if key in spec},
            "initContainers": [sidecar],
            "containers": [workload],
            "volumes": volumes,
        },
    }


async def two_sandboxes_act_as_one_kubernetes_account_and_one_loses_it(
    cluster: Cluster, sandbox: Sandboxes, *, reader_svid_secret: str
) -> None:
    """Two Sandboxes act as one shared ServiceAccount through the proxy's impersonation, and
    removing one's use rule refuses it on its next request while the other keeps the account.

    `kubectl auth whoami` is a SelfSubjectReview, so its answer is the username the API server
    authorized the request as: the impersonated account, not the proxy. The k3s API server keeps
    no audit log, so the proxy's half of the pair is checked in the decision history instead.
    """
    first, second = await asyncio.gather(sandbox("systest-account-first"), sandbox("systest-account-second"))
    suffix = secrets.token_hex(4)
    account, credential = f"shared-{suffix}", f"k8s-account-{suffix}"
    accounts = kubernetes_accounts_namespace(cluster.namespace)
    username = f"system:serviceaccount:{accounts}:{account}"
    # The later -n overrides the fixture namespace the driver passes.
    await cluster.kubectl(
        "create",
        "-n",
        accounts,
        "-f",
        "-",
        body={"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": account, "namespace": accounts}},
    )
    try:
        owners = _owned_by(first, second)
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "EgressCredential",
                "metadata": {"name": credential, "ownerReferences": owners},
                "spec": {
                    "description": f"Act as {accounts}/{account} on the local API server.",
                    "source": {"kubernetesServiceAccount": {"namespace": accounts, "name": account}},
                    "targets": [{"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}],
                },
            },
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "PolicySet",
                "metadata": {"name": credential, "ownerReferences": owners},
                "spec": {
                    "rules": [
                        {
                            "type": "egress",
                            "hosts": [KUBERNETES_HOST],
                            "clusterInternal": True,
                            "credentialRef": {"name": credential},
                        }
                    ]
                },
            },
        )
        revoked = await kubectl_create_binding(cluster, first, credential)
        await kubectl_create_binding(cluster, second, credential)

        with tracer.start_as_current_span(
            "system_test.egress.account.observe", record_exception=False, set_status_on_exception=False
        ):
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(BINDING_SECONDS), wait=wait_fixed(1), reraise=True
            ):
                with attempt:
                    for view in (first, second):
                        assert await _whoami(cluster, view, credential) == username
        for view in (first, second):
            acted = await decision_for(cluster, view, KUBERNETES_HOST, reader_svid_secret=reader_svid_secret)
            assert acted.outcome == Decision.OUTCOME_ALLOW, f"{acted!r}"
            assert acted.policy == credential, f"{acted!r}"
            assert acted.substituted, f"{acted!r}"
            assert (acted.impersonated.namespace, acted.impersonated.name) == (accounts, account), (
                f"the decision does not name the impersonated account: {acted!r}"
            )

        await cluster.kubectl("delete", "policybinding", revoked, "--wait=true")
        with tracer.start_as_current_span(
            "system_test.egress.account.revoke", record_exception=False, set_status_on_exception=False
        ):
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(BINDING_SECONDS), wait=wait_fixed(1), reraise=True
            ):
                with attempt:
                    after = datetime.now(UTC)
                    try:
                        still = await _whoami(cluster, first, credential)
                    except RuntimeError:
                        still = None
                    assert still is None, f"{first.target.name} still acts as {still} after losing its rule"
        refused = await decision_for(
            cluster, first, KUBERNETES_HOST, reader_svid_secret=reader_svid_secret, after=after
        )
        assert refused.outcome == Decision.OUTCOME_DENY, f"{refused!r}"
        assert refused.reason == Decision.REASON_NO_RULE, f"{refused!r}"
        assert await _whoami(cluster, second, credential) == username
    finally:
        await cluster.kubectl("delete", "serviceaccount", account, "-n", accounts, "--ignore-not-found")


async def a_command_box_presents_its_own_svid(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor, policy_objects: PolicyObjects
) -> None:
    """A box the sandbox Actions create for a Sandbox presents its own WorkloadCredential's SVID,
    subject the caller: rules, Kubernetes substitution and refusals as the caller, no credential in
    the workload container, and nothing once its record is revoked."""
    async with create_kubernetes_probe(cluster, view) as (configmap, namespace, expected, account, _policy_set):
        assert view.identity_name
        caller = view.identity_name
        await policy_objects.bind(view, "box", [{"type": "exact_actions", "actions": {_SANDBOX_GROUP: ["create"]}}])
        box = "svid-" + secrets.token_hex(4)
        pod = f"{caller}-{box}"

        async def in_box(*command: str) -> str:
            return await cluster.exec(pod, *command, container=_BOX_CONTAINER)

        try:
            printed = await cluster.exec(
                view.kubernetes_diagnostics.pod.name,
                CLI,
                "--json",
                *submission(
                    _SANDBOX_GROUP,
                    "create",
                    {"name": box, "template": svid_box.TEMPLATE},
                    title="Create an SVID box",
                    wait=True,
                ),
                container=_RUNNER_CONTAINER,
            )
            receipt = json_format.Parse(
                one(line for line in printed.splitlines() if line.startswith("{")), action_service_pb2.ActionRequest()
            )
            assert receipt.state == action_service_pb2.ACTION_STATE_SUCCEEDED, printed
            await cluster.kubectl("wait", "--for=condition=Ready", "pod/" + pod, "--timeout=120s")
            record = json.loads(await cluster.kubectl("get", "workloadcredentials", pod, "-o", "json"))
            box_identity = record["spec"]["subject"]["identity"]
            assert box_identity["name"] == caller
            assert box_identity["uid"]
            assert record["spec"]["pod"]["name"] == pod

            rules = json.loads(await in_box(CLI, "egress", "rules", "--json"))
            assert rules["identity"] == {"name": caller}
            credential = one(
                rule["credential"]
                for policy in rules["policies"]
                for rule in policy["rules"]
                if KUBERNETES_HOST in rule["hosts"]
            )
            assert credential["kubernetesServiceAccount"] == {
                "namespace": kubernetes_accounts_namespace(cluster.namespace),
                "name": account,
            }
            actual = await in_box(
                "kubectl",
                f"--server=https://{KUBERNETES_HOST}",
                f"--token={credential['placeholder']}",
                "get",
                "configmap",
                configmap,
                "-n",
                namespace,
                "-o",
                "jsonpath={.data.value}",
            )
            assert actual == expected
            curl = ("curl", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}", "--max-time", "15")
            assert await in_box(*curl, "http://unlisted.test/") == "403"
            await in_box(
                "/bin/bash",
                "-c",
                f"test ! -e {_BOX_SVID_DIR} && test ! -e /run/agentplane/environment-runtime-tls/tls.key",
            )

            # Revoked, as dispose does first; orphaning keeps the Pod, whose SVID now names no record.
            await cluster.kubectl("delete", "workloadcredentials", pod, "--cascade=orphan", "--wait=true")
            await in_box("/bin/bash", "-c", f"! {CLI} egress rules --json >/dev/null 2>&1")
        finally:
            await cluster.kubectl("delete", "pod", pod, "--wait=true", "--timeout=60s", "--ignore-not-found")
            await cluster.kubectl("delete", "workloadcredentials", pod, "--ignore-not-found")


async def a_standing_workload_authenticates_by_its_svid_and_its_prune_revokes(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> None:
    """Two replicas of a Deployment share a standing Identity WorkloadCredential: no owner and no
    binding, so its Secret name is stable and no Pod is proven. Both authenticate as its subject, and
    deleting the record revokes both."""
    name = "standing-" + secrets.token_hex(6)
    assert view.identity_name
    await cluster.kubectl(
        "create",
        "-f",
        "-",
        body={
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "WorkloadCredential",
            "metadata": {"name": name},
            "spec": {"subject": {"identity": {"name": view.identity_name}}},
        },
    )
    try:
        async for attempt in AsyncRetrying(stop=stop_after_delay(_STANDING_SECONDS), wait=wait_fixed(1), reraise=True):
            with attempt:
                status = json.loads(await cluster.kubectl("get", "workloadcredential", name, "-o", "json"))["status"]
                assert any(c["type"] == "Ready" and c["status"] == "True" for c in status["conditions"]), status
        assert status["secretName"] == f"svid-{name}"
        assert "acceptedBinding" not in status
        runner = json.loads(await cluster.kubectl("get", "pod", view.kubernetes_diagnostics.pod.name, "-o", "json"))
        box_labels = json.loads(await cluster.kubectl("get", "podtemplate", "sandbox", "-o", "json"))["template"][
            "metadata"
        ]["labels"]
        template = _standing_pod_spec(runner, {**box_labels, "app": name}, status["secretName"])
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name},
                "spec": {"replicas": 2, "selector": {"matchLabels": {"app": name}}, "template": template},
            },
        )
        await cluster.kubectl("rollout", "status", f"deployment/{name}", "--timeout=180s", timeout_s=200)
        pods = [
            pod["metadata"]["name"]
            for pod in json.loads(await cluster.kubectl("get", "pods", "-l", f"app={name}", "-o", "json"))["items"]
            if not pod["metadata"].get("deletionTimestamp")
        ]
        assert len(pods) == 2, pods
        for pod in pods:
            # Authenticated as the subject, whose policies name no such host.
            assert await _egress_refusal(cluster, pod, f"http://{UNLISTED_HOST}/") == "no-rule"
            # This Identity has no Kubernetes host grant, even when the request presents a placeholder.
            assert (
                await _egress_refusal(
                    cluster,
                    pod,
                    f"https://{KUBERNETES_HOST}/api",
                    f"Authorization: Bearer {placeholder_of('kubernetes-workload')}",
                )
                == "no-rule"
            )
        await cluster.kubectl("delete", "workloadcredential", name, "--wait=true", "--timeout=60s")
        for pod in pods:
            assert await _egress_refusal(cluster, pod, f"http://{UNLISTED_HOST}/") == "token-rejected"
    finally:
        await cluster.kubectl("delete", "deployment", name, "--ignore-not-found", "--wait=true", "--timeout=60s")
        await cluster.kubectl(
            "delete", "workloadcredential", name, "--ignore-not-found", "--wait=true", "--timeout=60s"
        )
