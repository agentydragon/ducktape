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
import textwrap
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.app.testing.client import Client
from agentplane.egress.api.admin.egress_admin_pb2 import Decision
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.deployment import runtime_grant
from agentplane.system_tests.driver.agent import Agent
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.scenarios.egress_platform import (
    BINDING_SECONDS,
    KUBERNETES_HOST,
    UNLISTED_HOST,
    create_kubernetes_probe,
    decision_for,
    kubectl_create_binding,
)
from agentplane.testing.scripted_model import Text
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace

# Generated protocol stubs import protobuf types that mypy resolves through direct deps.
# gazelle:include_dep @pypi//protobuf

# Anonymous `git` fetches from github.com (cluster/cdk8s/agentplane/egress.py, `GITHUB_CLONE_POLICY`).
GITHUB_CLONE = "github-clone"
PUBLIC_REPO = "https://github.com/agentydragon/ducktape"
# The app's default egress PolicySet. It allows model ingress; Kubernetes access is an explicit
# account-use grant in the identity catalog.
BASIC_POLICY = "basic"


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
    async with create_kubernetes_probe(cluster, view) as (configmap, namespace, expected_value, account, policy_set):
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

        refused = await decision_for(
            cluster, await client.get_environment(view.target), UNLISTED_HOST, reader_svid_secret="app-workload-tls"
        )
        assert refused.outcome == Decision.OUTCOME_DENY, f"{refused!r}"
        assert refused.reason == Decision.REASON_NO_RULE, f"{refused!r}"

        kubernetes = await decision_for(
            cluster, await client.get_environment(view.target), KUBERNETES_HOST, reader_svid_secret="app-workload-tls"
        )
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

    rules_command = "agentplane --json egress rules"
    rules_turn = await agent.run_shell(model, rules_command)
    assert rules_turn.shell_json(rules_command).get("identity") == {"name": view.identity_name}, rules_turn.transcript
    notifications_turn = await agent.run_shell(
        model, "agentplane notifications subscriptions --limit 1 >/dev/null && echo NOTIFICATIONS_GRPC_OK"
    )
    assert "NOTIFICATIONS_GRPC_OK" in notifications_turn.tool_outputs[-1], notifications_turn.transcript

    served = await decision_for(
        cluster,
        await client.get_environment(view.target),
        f"llm-ingress.{cluster.namespace}.svc.cluster.local",
        reader_svid_secret="app-workload-tls",
    )
    assert served.outcome == Decision.OUTCOME_ALLOW, f"{served!r}\n{turn.transcript}"
    assert served.method == "CONNECT", f"the model call did not traverse the proxy as CONNECT: {served!r}"
    assert not served.policy, f"the exact-origin SVID route should not use an egress policy: {served!r}"
    assert not served.substituted, f"the SVID route must not substitute a bearer: {served!r}"


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
    refused = await decision_for(
        cluster, await client.get_environment(view.target), host, reader_svid_secret="app-workload-tls"
    )
    assert refused.outcome == Decision.OUTCOME_DENY, f"an unbound sandbox was admitted: {refused!r}"

    # Exercise a live Kubernetes policy update; app grant authorization has its own
    # acceptance. The binding is owned by this exact Sandbox incarnation.
    await kubectl_create_binding(cluster, view, runtime_grant.POLICY_SET)
    # Observe the read-only request, not one replica's acknowledgement of the binding.
    with tracer.start_as_current_span(
        "system_test.egress.policy_binding.observe", record_exception=False, set_status_on_exception=False
    ):
        async for attempt in AsyncRetrying(stop=stop_after_delay(BINDING_SECONDS), wait=wait_fixed(1), reraise=True):
            with attempt:
                after = datetime.now(UTC)
                turn = await agent.run_shell(model, command)
                admitted = await decision_for(
                    cluster,
                    await client.get_environment(view.target),
                    host,
                    reader_svid_secret="app-workload-tls",
                    after=after,
                )
                assert admitted.outcome == Decision.OUTCOME_ALLOW, f"{admitted!r}\n{turn.transcript}"
                assert admitted.policy == runtime_grant.POLICY_SET, f"{admitted!r}"

    current = await client.get_environment(view.target)
    assert current.target.uid == view.target.uid
    assert current.kubernetes_diagnostics.HasField("pod")
    assert current.kubernetes_diagnostics.pod.uid == view.kubernetes_diagnostics.pod.uid, (
        "the live policy grant must take effect without replacing the running Pod"
    )
