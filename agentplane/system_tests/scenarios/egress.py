"""What a sandbox may reach, checked by running agents in it.

The probe runs a real model against a deployment. It states a goal and asks the agent to end with a
JSON report, rather than dictating a command and grepping prose: the agent chooses how, which is the
behaviour worth testing, and the answer stays machine-checkable. Every claim in that report is then
cross-checked against the proxy's decision history, which is the system's own record of what it
admitted -- an agent saying "I fetched it" is equally consistent with a request the proxy admitted,
one that never reached the proxy, and a model that ran nothing.

The Kubernetes assertion is the value of a fresh, unpredictable ConfigMap entry granted only to a
fixture ServiceAccount. The Sandbox Identity reaches it through an explicitly bound EgressCredential
and the proxy's credential substitution; no per-Sandbox ServiceAccount is involved.

What these encode is the deployed form of agentplane/egress/SPEC.md: a sandbox reaches what its
policies name and nothing else, and the model call uses the Sandbox SVID route through the proxy.
They exist because the last gap of this kind
-- a runner that dropped the proxy variables, so every call bypassed the proxy and hung, leaving an
empty history behind a green unit suite -- was caught by a person noticing.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import textwrap
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from google.protobuf import json_format
from more_itertools import one
from pydantic import BaseModel, ConfigDict, Field
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.action_service import action_service_pb2
from agentplane.app.testing.client import Client
from agentplane.crd_group import GROUP, VERSION
from agentplane.egress import sidecar_config
from agentplane.egress.egress_admin_pb2 import Decision
from agentplane.egress.resources import placeholder_of
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.system_tests.deployment import runtime_grant, svid_box
from agentplane.system_tests.driver import egress_decisions
from agentplane.system_tests.driver.agent import Agent
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios.mcp import CLI, PolicyObjects, submission
from agentplane.testing.scripted_model import Text
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace

# Anonymous `git` fetches from github.com (cluster/cdk8s/agentplane/egress.py, `GITHUB_CLONE_POLICY`).
GITHUB_CLONE = "github-clone"
PUBLIC_REPO = "https://github.com/agentydragon/ducktape"
# Named by no policy the deployment has, so it is refused for want of a rule rather than by one.
UNLISTED_HOST = "example.com"
# The app's default egress PolicySet. It allows model ingress; Kubernetes access is an explicit
# account-use grant in the identity catalog.
BASIC_POLICY = "basic"
KUBERNETES_HOST = "kubernetes.default.svc.cluster.local"
# The sandbox Actions' group key in the fixture deployment (agentplane/system_tests/deployment/fixture_chart.py),
# and a command box's workload container (cluster/cdk8s/agentplane/command_sandbox.py, `CONTAINER`).
_SANDBOX_GROUP = "sandbox"
_BOX_CONTAINER = "sandbox"
# The runner container, whose own `agentplane` CLI calls the Action Service as the Sandbox
# (cluster/cdk8s/agentplane/environment_sandbox.py).
_RUNNER_CONTAINER = "environment-runtime"
# Where the Action Service mounts a box's SVID, in the sidecar alone (agentplane/action_service/sandbox/inventory.go).
_BOX_SVID_DIR = "/run/agentplane/workload-svid"

# The proxy records a decision as it serves it; the app reads committed history over a separate hop, and a
# each replica observes bindings asynchronously; the scenario checks actual read-only egress.
DECISION_SECONDS = 30.0
BINDING_SECONDS = 60.0


def _probe(configmap: str, namespace: str) -> str:
    """Ask the agent to read an unpredictable value using its normal platform guidance."""
    return textwrap.dedent(f"""\
    Read key `value` from ConfigMap `{configmap}` in namespace `{namespace}` using the platform's
    normal Kubernetes access guidance.

    Also run `git ls-remote {PUBLIC_REPO}` (no clone) and try to fetch https://{UNLISTED_HOST}/.

    End with exactly one JSON object and nothing after it:

    {{"configmap_value": "the value you read, or null",
      "refs_ok": true or false,
      "unlisted_refused": true if the host was refused or failed, false if it succeeded}}
    """)


class Probe(BaseModel):
    """The report the probe prompt asks for."""

    model_config = ConfigDict(extra="ignore")

    configmap_value: str | None = Field(description="The value read from the requested ConfigMap.")
    refs_ok: bool = Field(description="Whether the public repository's refs came back.")
    unlisted_refused: bool = Field(description="Whether the host no policy names was refused.")


async def _decision_for(
    cluster: Cluster, client: Client, sandbox: str, uid: str, host: str, *, after: datetime | None = None
) -> Decision:
    """The newest decision for this live Sandbox Identity about `host`, after `after`."""
    with tracer.start_as_current_span(
        "system_test.egress.proxy_decision", record_exception=False, set_status_on_exception=False
    ):
        target = identity_pb2.EnvironmentTarget(namespace=cluster.namespace, name=sandbox, uid=uid)
        environment = await client.get_environment(target)
        assert environment.identity_name
        async with egress_decisions.watch_client(cluster) as (stub, caller):
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(DECISION_SECONDS), wait=wait_fixed(1), reraise=True
            ):
                with attempt:
                    decisions = await egress_decisions.snapshot_identity(stub, caller, environment.identity_name)
                    for decision in reversed(decisions):
                        if decision.host == host and (after is None or decision.at.ToDatetime(tzinfo=UTC) > after):
                            return decision
                    raise AssertionError(
                        f"no decision about {host} after {after}; the history contains {[d.host for d in decisions]}"
                    )
    raise AssertionError("unreachable: reraise=True either returns a decision or raises")


@asynccontextmanager
async def _create_kubernetes_probe(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> AsyncIterator[tuple[str, str, str, str, str]]:
    """Create a private ConfigMap and grant its exact get permission through the Sandbox Identity."""
    assert view.identity_name
    suffix = secrets.token_hex(4)
    name = f"{view.target.name}-kubernetes-probe"
    policy_set = f"k8s-probe-{suffix}"
    account_namespace = kubernetes_accounts_namespace(cluster.namespace)
    account = f"probe-{suffix}"
    value = secrets.token_urlsafe(32)
    owner_references = [
        {"apiVersion": SANDBOX_API_VERSION, "kind": SANDBOX_KIND, "name": view.target.name, "uid": str(view.target.uid)}
    ]
    resource_metadata: dict[str, object] = {"namespace": cluster.namespace, "ownerReferences": owner_references}
    named_metadata = {**resource_metadata, "name": name}
    await cluster.kubectl(
        "create",
        "-n",
        account_namespace,
        "-f",
        "-",
        body={"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": account}},
    )
    try:
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={"apiVersion": "v1", "kind": "ConfigMap", "metadata": named_metadata, "data": {"value": value}},
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": named_metadata,
                "rules": [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [name], "verbs": ["get"]}],
            },
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": named_metadata,
                "subjects": [{"kind": "ServiceAccount", "name": account, "namespace": account_namespace}],
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
            },
        )
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "EgressCredential",
                "metadata": {"name": policy_set, "ownerReferences": owner_references},
                "spec": {
                    "description": f"Read ConfigMap {cluster.namespace}/{name} as {account_namespace}/{account}.",
                    "source": {"kubernetesServiceAccount": {"namespace": account_namespace, "name": account}},
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
                "metadata": {"name": policy_set, "ownerReferences": owner_references},
                "spec": {
                    "rules": [
                        {
                            "type": "egress",
                            "hosts": [KUBERNETES_HOST],
                            "clusterInternal": True,
                            "credentialRef": {"name": policy_set},
                        }
                    ]
                },
            },
        )
        binding = await _kubectl_create_binding(cluster, view, policy_set)
        await cluster.kubectl("wait", "--for=condition=EgressReady", f"policybinding/{binding}", "--timeout=90s")
        yield name, cluster.namespace, value, account, policy_set
    finally:
        await cluster.kubectl(
            "delete", "serviceaccount", account, "-n", account_namespace, "--ignore-not-found", "--wait=true"
        )


async def a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    model: str,
) -> None:
    """The agent reads a private ConfigMap through its Identity's explicit account-use grant, while
    the same Sandbox reaches a named public host and is refused elsewhere."""
    view = await sandbox(f"systest-probe-{harness}", policies=[GITHUB_CLONE])
    async with _create_kubernetes_probe(cluster, view) as (configmap, namespace, expected_value, account, policy_set):
        agent = await Agent.create(
            client, dispatcher, sandbox=view.target.name, sandbox_uid=view.target.uid, harness=harness, model=model
        )
        turn = await agent.run(_probe(configmap, namespace))
        probe = turn.report(Probe)

        assert probe.configmap_value == expected_value, (
            f"the sandbox did not read the ConfigMap using its explicitly granted ServiceAccount; "
            f"expected {expected_value!r}, got {probe.configmap_value!r}:\n{turn.transcript}"
        )
        assert probe.refs_ok, f"the public repository was not reachable:\n{turn.transcript}"
        assert probe.unlisted_refused, f"{UNLISTED_HOST} was reachable:\n{turn.transcript}"

        refused = await _decision_for(cluster, client, view.target.name, view.target.uid, UNLISTED_HOST)
        assert refused.outcome == Decision.OUTCOME_DENY, f"{refused!r}"
        assert refused.reason == Decision.REASON_NO_RULE, f"{refused!r}"

        kubernetes = await _decision_for(cluster, client, view.target.name, view.target.uid, KUBERNETES_HOST)
        assert kubernetes.outcome == Decision.OUTCOME_ALLOW, f"{kubernetes!r}"
        assert kubernetes.policy == policy_set, f"{kubernetes!r}"
        assert kubernetes.substituted, f"kubectl reached the API server without credential substitution: {kubernetes!r}"
        assert (kubernetes.impersonated.namespace, kubernetes.impersonated.name) == (
            kubernetes_accounts_namespace(cluster.namespace),
            account,
        ), f"the decision does not name the fixture account: {kubernetes!r}"


async def the_model_call_itself_goes_through_the_proxy(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    view: environment_service_pb2.EnvironmentDescriptor,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    """The SVID route strips caller authorization and authenticates the destination TLS connection
    with the runner's Runtime certificate. The proxy admits the CONNECT under the Sandbox Identity;
    the ingress verifies the same live SVID and presents its server-held LiteLLM key
    on the separate internal hop.

    That makes the turn its own proof of reachability -- an agent that answers has been served -- and
    the subject-filtered decision history proves the CONNECT was admitted through the proxy. Both
    halves matter, because the failure this replaces was a
    sandbox reaching LiteLLM directly with the key in its own environment, which answers just as well
    and is what routing the model endpoint through the proxy exists to stop.

    The sandbox names no policy. The SVID route is an exact-origin platform route, and the service
    independently verifies the leaf before forwarding.
    """
    assert view.kubernetes_diagnostics.HasField("pod")
    model = scripted.model(view.kubernetes_diagnostics.pod.name, harness)
    agent = await Agent.create(
        client, dispatcher, sandbox=view.target.name, sandbox_uid=view.target.uid, harness=harness, model=offered_model
    )
    async with asyncio.TaskGroup() as turns:
        answered = turns.create_task(agent.run("Reply with the single word: ok."))
        await model.reply(await model.request(), Text("ok"))
    turn = answered.result()

    rules_turn = await agent.run_shell(model, "agentplane --json egress rules")
    rules_output = rules_turn.tool_outputs[-1]
    start = rules_output.find("{")
    assert start >= 0, rules_turn.transcript
    rules, _ = json.JSONDecoder().raw_decode(rules_output[start:])
    assert rules["identity"] == {"name": view.identity_name}, rules_turn.transcript
    notifications_turn = await agent.run_shell(
        model, "agentplane notifications subscriptions --limit 1 >/dev/null && echo NOTIFICATIONS_GRPC_OK"
    )
    assert "NOTIFICATIONS_GRPC_OK" in notifications_turn.tool_outputs[-1], notifications_turn.transcript

    served = await _decision_for(
        cluster, client, view.target.name, view.target.uid, f"llm-ingress.{cluster.namespace}.svc.cluster.local"
    )
    assert served.outcome == Decision.OUTCOME_ALLOW, f"{served!r}\n{turn.transcript}"
    assert served.method == "CONNECT", f"the model call did not traverse the proxy as CONNECT: {served!r}"
    assert not served.policy, f"the exact-origin SVID route should not use an egress policy: {served!r}"
    assert not served.substituted, f"the SVID route must not substitute a bearer: {served!r}"


async def _kubectl_create_binding(
    cluster: Cluster, sandbox: environment_service_pb2.EnvironmentDescriptor, policy_set: str
) -> str:
    """Bind `policy_set` to the Sandbox Identity, owned by the Sandbox; return the binding name."""
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicyBinding",
        "metadata": {
            "generateName": f"{sandbox.target.name}-",
            "ownerReferences": [
                {
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": SANDBOX_KIND,
                    "name": sandbox.target.name,
                    "uid": str(sandbox.target.uid),
                }
            ],
        },
        "spec": {"subject": {"identity": {"name": sandbox.identity_name}}, "policySets": [policy_set]},
    }
    with tracer.start_as_current_span(
        "system_test.egress.policy_binding.apply", record_exception=False, set_status_on_exception=False
    ):
        return await cluster.kubectl("create", "-f", "-", "-o", "jsonpath={.metadata.name}", body=body)


async def _granted_request(cluster: Cluster) -> tuple[str, int, str]:
    """The intercepted destination the fixture's runtime-grant PolicySet admits."""
    result = await cluster.kubectl("get", "policyset", runtime_grant.POLICY_SET, "-o", "json", timeout_s=20)
    policy = json.loads(result)
    rule = policy["spec"]["rules"][0]
    return rule["hosts"][0], int(policy["metadata"]["annotations"][runtime_grant.PORT_ANNOTATION]), rule["paths"][0]


async def a_policy_granted_after_the_sandbox_is_running_takes_effect(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    """Binding at runtime is a live grant, not a restart: one sandbox, refused and then admitted."""
    host, port, path = await _granted_request(cluster)
    view = await sandbox(f"systest-bind-{harness}")
    assert view.kubernetes_diagnostics.HasField("pod")
    model = scripted.model(view.kubernetes_diagnostics.pod.name, harness)
    agent = await Agent.create(
        client, dispatcher, sandbox=view.target.name, sandbox_uid=view.target.uid, harness=harness, model=offered_model
    )
    command = f"curl -sS -o /dev/null -w '%{{http_code}}\\n' http://{host}:{port}{path}"

    await agent.run_shell(model, command)
    refused = await _decision_for(cluster, client, view.target.name, view.target.uid, host)
    assert refused.outcome == Decision.OUTCOME_DENY, f"an unbound sandbox was admitted: {refused!r}"

    # The app's grant route needs an operator login, which this agent-token suite does not hold, so
    # the binding is written as an operator would with kubectl, owned by the Sandbox.
    await _kubectl_create_binding(cluster, view, runtime_grant.POLICY_SET)
    # Observe the read-only request, not one replica's acknowledgement of the binding.
    with tracer.start_as_current_span(
        "system_test.egress.policy_binding.observe", record_exception=False, set_status_on_exception=False
    ):
        async for attempt in AsyncRetrying(stop=stop_after_delay(BINDING_SECONDS), wait=wait_fixed(1), reraise=True):
            with attempt:
                after = datetime.now(UTC)
                turn = await agent.run_shell(model, command)
                admitted = await _decision_for(cluster, client, view.target.name, view.target.uid, host, after=after)
                assert admitted.outcome == Decision.OUTCOME_ALLOW, f"{admitted!r}\n{turn.transcript}"
                assert admitted.policy == runtime_grant.POLICY_SET, f"{admitted!r}"


def _owned_by(*sandboxes: environment_service_pb2.EnvironmentDescriptor) -> list[dict[str, str]]:
    """Owner references that collect an object once every one of these Sandboxes is gone."""
    return [
        {"apiVersion": SANDBOX_API_VERSION, "kind": SANDBOX_KIND, "name": view.target.name, "uid": str(view.target.uid)}
        for view in sandboxes
    ]


async def _whoami(cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor, credential: str) -> str:
    """The username the API server sees for a `kubectl auth whoami` the Sandbox sends through its proxy."""
    return await cluster.kubectl(
        "exec",
        view.kubernetes_diagnostics.pod.name,
        "-c",
        "environment-runtime",
        "--",
        "kubectl",
        f"--server=https://{KUBERNETES_HOST}",
        f"--token={placeholder_of(credential)}",
        "auth",
        "whoami",
        "-o",
        "jsonpath={.status.userInfo.username}",
    )


async def two_sandboxes_act_as_one_kubernetes_account_and_one_loses_it(
    cluster: Cluster, client: Client, sandbox: Sandboxes
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
        revoked = await _kubectl_create_binding(cluster, first, credential)
        await _kubectl_create_binding(cluster, second, credential)

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
            acted = await _decision_for(cluster, client, view.target.name, view.target.uid, KUBERNETES_HOST)
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
        refused = await _decision_for(
            cluster, client, first.target.name, first.target.uid, KUBERNETES_HOST, after=after
        )
        assert refused.outcome == Decision.OUTCOME_DENY, f"{refused!r}"
        assert refused.reason == Decision.REASON_NO_RULE, f"{refused!r}"
        assert await _whoami(cluster, second, credential) == username
    finally:
        await cluster.kubectl("delete", "serviceaccount", account, "-n", accounts, "--ignore-not-found")


# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf


async def a_command_box_presents_its_own_svid(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor, policy_objects: PolicyObjects
) -> None:
    """A box the sandbox Actions create for a Sandbox presents its own WorkloadCredential's SVID,
    subject the caller: rules, Kubernetes substitution and refusals as the caller, no credential in
    the workload container, and nothing once its record is revoked."""
    async with _create_kubernetes_probe(cluster, view) as (configmap, namespace, expected, account, _policy_set):
        assert view.identity_name
        caller = view.identity_name
        await policy_objects.bind(view, "box", [{"type": "exact_actions", "actions": {_SANDBOX_GROUP: ["create"]}}])
        box = "svid-" + secrets.token_hex(4)
        pod = f"{caller}-{box}"

        async def in_box(*command: str) -> str:
            return await cluster.kubectl("exec", pod, "-c", _BOX_CONTAINER, "--", *command)

        try:
            printed = await cluster.kubectl(
                "exec",
                view.kubernetes_diagnostics.pod.name,
                "-c",
                _RUNNER_CONTAINER,
                "--",
                CLI,
                "--json",
                *submission(
                    _SANDBOX_GROUP,
                    "create",
                    {"name": box, "template": svid_box.TEMPLATE},
                    title="Create an SVID box",
                    wait=True,
                ),
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


# The sidecar's listener (agentplane/egress/sidecar_config.py `Settings.listen_address`).
_SIDECAR_PROXY = "http://127.0.0.1:3128"
_STANDING_SECONDS = 120.0


async def _egress_refusal(cluster: Cluster, pod: str, url: str, *headers: str) -> str | None:
    """The proxy's refusal reason for `url` from `pod`'s workload container; None when it refused nothing."""
    response = await cluster.kubectl(
        "exec",
        pod,
        "-c",
        "workload",
        "--",
        "/bin/bash",
        "-c",
        """response=$("$0" "$@")
status=$?
if [ "$status" -eq 56 ] && printf '%s\\n' "$response" | grep -Eiq '^[[:space:]]*X-Agentplane-Egress:[[:space:]]*denied;[[:space:]]*reason='; then
  printf '%s\\n' "$response"
  exit 0
fi
printf '%s\\n' "$response"
exit "$status""",
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
