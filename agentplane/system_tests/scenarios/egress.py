"""What a sandbox may reach, checked by running agents in it.

The probe runs a real model against a deployment. It states a goal and asks the agent to end with a
JSON report, rather than dictating a command and grepping prose: the agent chooses how, which is the
behaviour worth testing, and the answer stays machine-checkable. Every claim in that report is then
cross-checked against the proxy's decision history, which is the system's own record of what it
admitted -- an agent saying "I fetched it" is equally consistent with a request the proxy admitted,
one that never reached the proxy, and a model that ran nothing.

The reported username is the load-bearing assertion. The probe credential's account
(`probe_credential`) can only come back if the sandbox asked the proxy what it may present and was
told, sent a placeholder it cannot itself resolve, the sidecar carried the Pod's token, the proxy
admitted the request and swapped in the token it holds for that account, and the API server
authenticated it. No part of that can be faked by a model being agreeable, and the placeholder
appears nowhere in the prompt -- the agent has to go and find it.

What these encode is the deployed form of agentplane/egress/SPEC.md: a sandbox reaches what its
policies name and nothing else, with the credential substituted at the proxy, and the model call is
one of those requests rather than an exception to them. They exist because the last gap of this kind
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
from agentplane.app.testing.models import SandboxView
from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.decisions import Decision, Outcome
from agentplane.proto import runner_pb2
from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.system_tests import probe_credential, runtime_grant
from agentplane.system_tests.agent import Agent
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.tracing import tracer
from agentplane.testing.scripted_model import Text

# Anonymous `git` fetches from github.com (cluster/cdk8s/agentplane/egress.py, `GITHUB_CLONE_POLICY`).
GITHUB_CLONE = "github-clone"
PUBLIC_REPO = "https://github.com/agentydragon/ducktape"
# Named by no policy the deployment has, so it is refused for want of a rule rather than by one.
UNLISTED_HOST = "example.com"
# The authenticated model ingress, on the same egress path as everything else
# (cluster/k8s/agentplane-staging/agentplane-staging.k8s.yaml, PolicySet "basic"). It holds
# the LiteLLM key and is bound by the app's default PolicySets rather than by a caller,
# because an agent that cannot reach it has nothing to run -- so a sandbox that names no policy
# still has this one.
BASIC_POLICY = "basic"

# The proxy records a decision as it serves it; the app reads committed history over a separate hop, and a
# each replica observes bindings asynchronously; the scenario checks actual read-only egress.
DECISION_SECONDS = 30.0
BINDING_SECONDS = 60.0


def _probe(rules_url: str) -> str:
    """Where a sandbox asks what it may reach. The prompt names `rules_url` and nothing about the
    credential: the agent has to discover both what to present and how, which is the property under
    test. The endpoint reports each target's header, the shape of its value and its scheme, so an agent
    does not have to guess `Bearer` -- both harnesses sent the placeholder bare and were refused when it
    did not."""
    return textwrap.dedent(f"""\
    You are inside a sandbox whose outbound network goes through a proxy. Work out what it lets you
    do, then report. Do all four steps, and do not stop early if one fails -- a failure is a result.

    1. Ask the proxy what you are allowed to reach: GET {rules_url} with Authorization: Bearer
       agentplane-credential-agentplane-workload (a known inert placeholder). It answers with the policies
       that apply to you. Some rules name a credential you do not hold: a placeholder, and every
       target it may be presented at -- a header and the shape of its value -- which the proxy
       swaps for the real value on its way out.
    2. Fetch the refs of the public repository {PUBLIC_REPO} (git ls-remote is enough; no clone).
    3. Ask the Kubernetes API which account you are authenticated as: POST
       https://{probe_credential.HOST}{probe_credential.WHOAMI_PATH} with the JSON body
       {{"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}}, presenting the
       credential the rules name for that host and path exactly as step 1 described it: the rules give
       the header, the shape of the value and, where the shape has one, the scheme. Build the value
       from those and put the placeholder in verbatim -- it is not a secret, and it is all you get.
    4. Try to fetch https://{UNLISTED_HOST}/ and see whether you are allowed to.

    End your final message with exactly one JSON object and nothing after it:

    {{"placeholder": "the placeholder you presented in step 3, or null",
      "refs_ok": true or false,
      "username": "the status.userInfo.username the API reported, or null",
      "unlisted_refused": true if step 4 was refused or failed, false if it succeeded}}
    """)


class Probe(BaseModel):
    """The report the probe prompt asks for."""

    model_config = ConfigDict(extra="ignore")

    placeholder: str | None = Field(description="What the agent learned to present, from the rules endpoint.")
    refs_ok: bool = Field(description="Whether the public repository's refs came back.")
    username: str | None = Field(description="The account the API server reported for the substituted credential.")
    unlisted_refused: bool = Field(description="Whether the host no policy names was refused.")


async def _decision_for(client: Client, sandbox: str, host: str, *, after: datetime | None = None) -> Decision:
    """The newest decision about `host`, once the proxy has one later than `after`."""
    with tracer.start_as_current_span(
        "system_test.egress.proxy_decision", record_exception=False, set_status_on_exception=False
    ):
        async for attempt in AsyncRetrying(stop=stop_after_delay(DECISION_SECONDS), wait=wait_fixed(1), reraise=True):
            with attempt:
                decisions = await client.decisions(sandbox)
                for decision in reversed(decisions):
                    if decision.host == host and (after is None or decision.at > after):
                        return decision
                raise AssertionError(
                    f"no decision about {host} after {after}; the history contains {[d.host for d in decisions]}"
                )
    raise AssertionError("unreachable: reraise=True either returns a decision or raises")


async def a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
    cluster: Cluster, client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, model: str
) -> None:
    """The whole path in one turn: the agent asks what it may reach, uses the credential it is told
    about without ever holding it, is authenticated as that credential's account, and is refused
    everywhere its policies do not name -- and the proxy agrees on every count.

    Nothing here tells the agent the placeholder. If it comes back as the account, discovery worked.
    """
    view = await sandbox(f"systest-probe-{harness}", policies=[probe_credential.POLICY_SET, GITHUB_CLONE])
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
    turn = await agent.run(_probe(f"http://egress.{cluster.namespace}.svc.cluster.local/v1/rules"))
    probe = turn.report(Probe)

    assert probe.placeholder, f"the sandbox could not learn what credential to present:\n{turn.transcript}"
    assert probe.refs_ok, f"the public repository was not reachable:\n{turn.transcript}"
    # `system:serviceaccount:<namespace>:<name>`, in the fixture's egress credentials namespace.
    assert (probe.username or "").endswith(f":{probe_credential.SERVICE_ACCOUNT}"), (
        f"the API server reported {probe.username!r}, so the credential the sandbox discovered did not "
        f"resolve to the probe's account:\n{turn.transcript}"
    )
    assert probe.unlisted_refused, f"{UNLISTED_HOST} was reachable:\n{turn.transcript}"

    admitted = await _decision_for(client, view.name, probe_credential.HOST)
    assert admitted.outcome is Outcome.ALLOW, f"{admitted!r}"
    assert admitted.policy == probe_credential.POLICY_SET, f"{admitted!r}"
    assert admitted.substituted, f"the API call was admitted with no credential substituted: {admitted!r}"

    refused = await _decision_for(client, view.name, UNLISTED_HOST)
    assert refused.outcome is Outcome.DENY, f"{refused!r}"
    assert refused.reason == "no-rule", f"{refused!r}"


async def the_model_call_itself_goes_through_the_proxy(
    cluster: Cluster,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    """The runner holds only an inert placeholder, not the LiteLLM key, so a turn happens at all only
    if the proxy admits the model call and substitutes its sidecar-only workload token for the
    authenticated ingress. The ingress then verifies that workload identity and presents its
    server-held LiteLLM key on the separate internal hop.

    That makes the turn its own proof of reachability -- an agent that answers has been served -- and
    leaves the decision to say *how*: through the proxy with a credential substituted, rather than
    around it on a key the sandbox held. Both halves matter, because the failure this replaces was a
    sandbox reaching LiteLLM directly with the key in its own environment, which answers just as well
    and is what routing the model endpoint through the proxy exists to stop.

    The sandbox names no policy. `basic` reaching the ingress anyway is what the app's
    default PolicySets are for.
    """
    view = await sandbox(f"systest-model-{harness}")
    assert view.pod is not None
    model = scripted.model(view.pod.name, harness)
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=offered_model)
    async with asyncio.TaskGroup() as turns:
        answered = turns.create_task(agent.run("Reply with the single word: ok."))
        await model.reply(await model.request(), Text("ok"))
    turn = answered.result()

    served = await _decision_for(client, view.name, f"llm-ingress.{cluster.namespace}.svc.cluster.local")
    assert served.outcome is Outcome.ALLOW, f"{served!r}\n{turn.transcript}"
    assert served.policy == BASIC_POLICY, f"the model call was admitted by another policy: {served!r}"
    assert served.substituted, (
        f"the model call was admitted with no credential substituted, so the sandbox did not present "
        f"only the inert placeholder to the authenticated ingress: {served!r}"
    )


async def _kubectl_create_binding(cluster: Cluster, sandbox: SandboxView, policy_set: str) -> None:
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PolicyBinding",
        "metadata": {
            "generateName": f"{sandbox.name}-",
            "ownerReferences": [
                {"apiVersion": SANDBOX_API_VERSION, "kind": SANDBOX_KIND, "name": sandbox.name, "uid": str(sandbox.uid)}
            ],
        },
        "spec": {"subject": sandbox.service_account.model_dump(), "policySets": [policy_set]},
    }
    with tracer.start_as_current_span(
        "system_test.egress.policy_binding.apply", record_exception=False, set_status_on_exception=False
    ):
        await cluster.kubectl("create", "-f", "-", body=body)


async def _granted_request(cluster: Cluster) -> tuple[str, str]:
    """The host and path the fixture's runtime-grant PolicySet admits."""
    result = await cluster.kubectl("get", "policyset", runtime_grant.POLICY_SET, "-o", "json", timeout_s=20)
    rule = json.loads(result)["spec"]["rules"][0]
    return rule["hosts"][0], rule["paths"][0]


async def a_policy_granted_after_the_sandbox_is_running_takes_effect(
    cluster: Cluster,
    client: Client,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    """Binding at runtime is a live grant, not a restart: one sandbox, refused and then admitted."""
    host, path = await _granted_request(cluster)
    view = await sandbox(f"systest-bind-{harness}")
    assert view.pod is not None
    model = scripted.model(view.pod.name, harness)
    agent = await Agent.open(client, sandbox=view.name, harness=harness, model=offered_model)
    command = f"curl -sS -o /dev/null -w '%{{http_code}}\\n' http://{host}{path}"

    await agent.run_shell(model, command)
    refused = await _decision_for(client, view.name, host)
    assert refused.outcome is Outcome.DENY, f"an unbound sandbox was admitted: {refused!r}"

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
                admitted = await _decision_for(client, view.name, host, after=after)
                assert admitted.outcome is Outcome.ALLOW, f"{admitted!r}\n{turn.transcript}"
                assert admitted.policy == runtime_grant.POLICY_SET, f"{admitted!r}"


# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
