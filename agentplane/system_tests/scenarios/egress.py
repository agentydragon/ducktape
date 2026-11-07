"""What a sandbox may reach, checked by running agents in it.

The probe runs a real model against a deployment. It states a goal and asks the agent to end with a
JSON report, rather than dictating a command and grepping prose: the agent chooses how, which is the
behaviour worth testing, and the answer stays machine-checkable. Every claim in that report is then
cross-checked against the proxy's decision history, which is the system's own record of what it
admitted -- an agent saying "I fetched it" is equally consistent with a request the proxy admitted,
one that never reached the proxy, and a model that ran nothing.

The Kubernetes assertion is the value of a fresh, unpredictable ConfigMap entry granted only to
the Sandbox's own ServiceAccount. Reading it proves the model used the platform's Kubernetes
guidance, the projected workload token, and the proxy's credential substitution.

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
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.egress_admin_pb2 import Decision
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.system_tests.deployment import runtime_grant
from agentplane.system_tests.driver import egress_decisions
from agentplane.system_tests.driver.agent import Agent
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.testing.scripted_model import Text

# Anonymous `git` fetches from github.com (cluster/cdk8s/agentplane/egress.py, `GITHUB_CLONE_POLICY`).
GITHUB_CLONE = "github-clone"
PUBLIC_REPO = "https://github.com/agentydragon/ducktape"
# Named by no policy the deployment has, so it is refused for want of a rule rather than by one.
UNLISTED_HOST = "example.com"
# The app's default egress PolicySet. It allows model ingress and the Kubernetes API without an
# additional Sandbox policy.
BASIC_POLICY = "basic"
KUBERNETES_HOST = "kubernetes.default.svc.cluster.local"

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
    """The newest decision for this live Sandbox ServiceAccount about `host`, after `after`."""
    with tracer.start_as_current_span(
        "system_test.egress.proxy_decision", record_exception=False, set_status_on_exception=False
    ):
        target = identity_pb2.EnvironmentTarget(namespace=cluster.namespace, name=sandbox, uid=uid)
        environment = await client.get_environment(target)
        subject = environment.execution_account.account
        assert subject.namespace == cluster.namespace
        assert subject.name == sandbox
        async with egress_decisions.watch_client(cluster) as (stub, caller):
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(DECISION_SECONDS), wait=wait_fixed(1), reraise=True
            ):
                with attempt:
                    decisions = await egress_decisions.snapshot(stub, caller, subject)
                    for decision in reversed(decisions):
                        if decision.host == host and (after is None or decision.at.ToDatetime(tzinfo=UTC) > after):
                            return decision
                    raise AssertionError(
                        f"no decision about {host} after {after}; the history contains {[d.host for d in decisions]}"
                    )
    raise AssertionError("unreachable: reraise=True either returns a decision or raises")


async def _create_kubernetes_probe(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> tuple[str, str, str]:
    """Create a private ConfigMap and a get-only grant for this Sandbox's exact ServiceAccount."""
    account = view.execution_account.account
    if account.namespace != view.target.namespace or account.namespace != cluster.namespace:
        raise AssertionError(
            f"fixture expected the Sandbox's own ServiceAccount and namespace to match: "
            f"{account.namespace}/{account.name}, Sandbox {view.target.namespace}/{view.target.name}, "
            f"fixture namespace {cluster.namespace}"
        )

    name = f"{view.target.name}-kubernetes-probe"
    value = secrets.token_urlsafe(32)
    metadata: dict[str, object] = {
        "namespace": account.namespace,
        "ownerReferences": [
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": SANDBOX_KIND,
                "name": view.target.name,
                "uid": str(view.target.uid),
            }
        ],
    }
    named_metadata = metadata.copy()
    named_metadata["name"] = name
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
            "subjects": [{"kind": "ServiceAccount", "name": account.name, "namespace": account.namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
        },
    )
    return name, account.namespace, value


async def a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    model: str,
) -> None:
    """The agent reads a private ConfigMap using its own projected ServiceAccount, while the same
    Sandbox reaches a named public host and is refused elsewhere."""
    view = await sandbox(f"systest-probe-{harness}", policies=[GITHUB_CLONE])
    configmap, namespace, expected_value = await _create_kubernetes_probe(cluster, view)
    agent = await Agent.create(
        client, dispatcher, sandbox=view.target.name, sandbox_uid=view.target.uid, harness=harness, model=model
    )
    turn = await agent.run(_probe(configmap, namespace))
    probe = turn.report(Probe)

    assert probe.configmap_value == expected_value, (
        f"the sandbox did not read the ConfigMap using its own ServiceAccount; "
        f"expected {expected_value!r}, got {probe.configmap_value!r}:\n{turn.transcript}"
    )
    assert probe.refs_ok, f"the public repository was not reachable:\n{turn.transcript}"
    assert probe.unlisted_refused, f"{UNLISTED_HOST} was reachable:\n{turn.transcript}"

    refused = await _decision_for(cluster, client, view.target.name, view.target.uid, UNLISTED_HOST)
    assert refused.outcome == Decision.OUTCOME_DENY, f"{refused!r}"
    assert refused.reason == "no-rule", f"{refused!r}"

    kubernetes = await _decision_for(cluster, client, view.target.name, view.target.uid, KUBERNETES_HOST)
    assert kubernetes.outcome == Decision.OUTCOME_ALLOW, f"{kubernetes!r}"
    assert kubernetes.policy == BASIC_POLICY, f"{kubernetes!r}"
    assert kubernetes.substituted, f"kubectl reached the API server without credential substitution: {kubernetes!r}"


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
    with the runner's Runtime certificate. The proxy admits the CONNECT under the Sandbox's own
    ServiceAccount; the ingress verifies the same live SVID and presents its server-held LiteLLM key
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
    assert rules["subject"] == {
        "namespace": view.execution_account.account.namespace,
        "name": view.execution_account.account.name,
    }, rules_turn.transcript
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
) -> None:
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
        "spec": {
            "subject": {
                "namespace": sandbox.execution_account.account.namespace,
                "name": sandbox.execution_account.account.name,
            },
            "policySets": [policy_set],
        },
    }
    with tracer.start_as_current_span(
        "system_test.egress.policy_binding.apply", record_exception=False, set_status_on_exception=False
    ):
        await cluster.kubectl("create", "-f", "-", body=body)


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


# `session_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
