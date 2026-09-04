"""What a sandbox may reach, checked against a deployed Agentplane by running real agents in it.

A scenario states a goal and asks the agent to end with a JSON report, rather than dictating a
command and grepping prose: the agent chooses how, which is the behaviour worth testing, and the
answer stays machine-checkable. Every claim in that report is then cross-checked against the proxy's
decision ring, which is the system's own record of what it served -- an agent saying "I fetched it"
is equally consistent with a request the proxy admitted, one that never reached the proxy, and a
model that ran nothing.

The reported username is the load-bearing assertion. `agentydragon-agent` can only come back if the
sandbox asked the proxy what it may present and was told, sent a placeholder it cannot itself
resolve, the sidecar carried the Pod's token, the proxy admitted the request and swapped the real
PAT in, and GitHub authenticated it. No part of that can be faked by a model being agreeable, and
the placeholder appears nowhere in the prompt -- the agent has to go and find it.

E3's acceptance in x/agentplane/plans/egress_proxy.md is what these encode. They exist because the
last gap of this kind -- a runner that dropped the proxy variables, so every call bypassed the proxy
and hung, leaving an empty ring behind a green unit suite -- was caught by a person noticing.
"""

from __future__ import annotations

import textwrap
from collections.abc import Awaitable, Callable
from datetime import datetime

import pytest_bazel
from pydantic import BaseModel, ConfigDict, Field
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from x.agentplane.acceptance.agent import Agent
from x.agentplane.app.api import Provider
from x.agentplane.app.client import Client
from x.agentplane.app.decisions import Decision, Outcome
from x.agentplane.app.inventory import SandboxView

# Staging's seeded policy and the credential it substitutes
# (cluster/k8s/agentplane-staging/egress/egresspolicy-github-public.yaml).
GITHUB_PUBLIC = "github-public"
GITHUB_HOST = "github.com"
GITHUB_API_HOST = "api.github.com"
PUBLIC_REPO = "https://github.com/agentydragon/ducktape"
# Where a sandbox asks what it may reach (C11). The prompt names this and nothing about the
# credential: the agent has to discover both what to present and how, which is the property under
# test. Node H is what makes the second half discoverable -- the endpoint reports each target's
# header, the shape of its value and its scheme, so an agent no longer has to guess that GitHub
# wants `Bearer` (both harnesses sent the placeholder bare and were refused when it did not).
RULES_URL = "https://egress.agentplane.internal/v1/rules"
# Whose PAT the policy substitutes: the identity GitHub reports back if substitution worked.
BOT_LOGIN = "agentydragon-agent"
# Named by no policy staging has, so it is refused for want of a rule rather than by one.
UNLISTED_HOST = "example.com"

# The proxy records a decision as it serves it; the app reads the ring over a separate hop, and a
# binding's Active condition is written by the proxy's informer rather than by the grant itself.
DECISION_SECONDS = 30.0
BINDING_SECONDS = 60.0

Sandboxes = Callable[..., Awaitable[SandboxView]]

PROBE = textwrap.dedent(f"""\
    You are inside a sandbox whose outbound network goes through a proxy. Work out what it lets you
    do, then report. Do all four steps, and do not stop early if one fails -- a failure is a result.

    1. Ask the proxy what you are allowed to reach: GET {RULES_URL}. It answers with the policies
       that apply to you. Some rules name a credential you do not hold: a placeholder, and every
       target it may be presented at -- a header and the shape of its value -- which the proxy
       swaps for the real value on its way out.
    2. Fetch the refs of the public repository {PUBLIC_REPO} (git ls-remote is enough; no clone).
    3. Ask the GitHub API which account you are authenticated as: GET https://{GITHUB_API_HOST}/user,
       presenting the credential exactly as step 1 described it: the rules give the header, the
       shape of the value and, where the shape has one, the scheme. Build the value from those and
       put the placeholder in verbatim -- it is not a secret, and it is all you get.
    4. Try to fetch https://{UNLISTED_HOST}/ and see whether you are allowed to.

    End your final message with exactly one JSON object and nothing after it:

    {{"placeholder": "the placeholder string step 1 gave you, or null",
      "refs_ok": true or false,
      "username": "the login the API reported, or null",
      "unlisted_refused": true if step 4 was refused or failed, false if it succeeded}}
    """)


class Probe(BaseModel):
    """The report the probe prompt asks for."""

    model_config = ConfigDict(extra="ignore")

    placeholder: str | None = Field(description="What the agent learned to present, from the rules endpoint.")
    refs_ok: bool = Field(description="Whether the public repository's refs came back.")
    username: str | None = Field(description="The login GitHub reported for the substituted credential.")
    unlisted_refused: bool = Field(description="Whether the host no policy names was refused.")


def _verbatim(command: str) -> str:
    return f"Run exactly this command and report its output verbatim: {command}"


async def _decision_for(client: Client, sandbox: str, host: str, *, after: datetime | None = None) -> Decision:
    """The newest decision about `host`, once the proxy has one later than `after`."""
    async for attempt in AsyncRetrying(stop=stop_after_delay(DECISION_SECONDS), wait=wait_fixed(1), reraise=True):
        with attempt:
            decisions = await client.decisions(sandbox)
            for decision in reversed(decisions):
                if decision.host == host and (after is None or decision.at > after):
                    return decision
            raise AssertionError(
                f"no decision about {host} after {after}; the ring holds {[d.host for d in decisions]}"
            )
    raise AssertionError("unreachable: reraise=True either returns a decision or raises")


async def _active(client: Client, sandbox: str, binding: str) -> None:
    """Wait for the proxy to write the binding's Active condition: until it has, the grant is not yet
    the proxy's picture of the world, and a request would be judged against the old one."""
    async for attempt in AsyncRetrying(stop=stop_after_delay(BINDING_SECONDS), wait=wait_fixed(1), reraise=True):
        with attempt:
            for view in await client.bindings(sandbox):
                if view.name == binding:
                    if not view.active:
                        raise AssertionError(f"{binding} is {view.active_reason}: {view.active_message}")
                    return
            raise AssertionError(f"{binding} is not among the bindings naming {sandbox}")


async def test_a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
    client: Client, sandbox: Sandboxes, provider: Provider, model: str
) -> None:
    """E3 and C11's acceptance in one turn: the agent asks what it may reach, uses the credential it
    is told about without ever holding it, is authenticated as the bot, and is refused everywhere the
    policy does not name -- and the proxy agrees on every count.

    Nothing here tells the agent the placeholder. If it comes back as the bot, discovery worked.
    """
    view = await sandbox(f"accept-probe-{provider}", policies=[GITHUB_PUBLIC])
    agent = await Agent.open(client, sandbox=view.name, provider=provider, model=model)
    turn = await agent.run(PROBE)
    probe = turn.report(Probe)

    assert probe.placeholder, f"the sandbox could not learn what credential to present:\n{turn.transcript}"
    assert probe.refs_ok, f"the public repository was not reachable:\n{turn.transcript}"
    assert probe.username == BOT_LOGIN, (
        f"GitHub reported {probe.username!r}, so the credential the sandbox discovered did not "
        f"resolve to the bot:\n{turn.transcript}"
    )
    assert probe.unlisted_refused, f"{UNLISTED_HOST} was reachable:\n{turn.transcript}"

    admitted = await _decision_for(client, view.name, GITHUB_API_HOST)
    assert admitted.outcome is Outcome.ALLOW, f"{admitted!r}"
    assert admitted.policy == GITHUB_PUBLIC, f"{admitted!r}"
    assert admitted.substituted, f"the API call was admitted with no credential substituted: {admitted!r}"

    refused = await _decision_for(client, view.name, UNLISTED_HOST)
    assert refused.outcome is Outcome.DENY, f"{refused!r}"
    assert refused.reason == "no-rule", f"{refused!r}"


async def test_a_policy_granted_after_the_sandbox_is_running_takes_effect(
    client: Client, sandbox: Sandboxes, provider: Provider, model: str
) -> None:
    """Binding at runtime is a live grant, not a restart: one sandbox, refused and then admitted."""
    view = await sandbox(f"accept-bind-{provider}")
    agent = await Agent.open(client, sandbox=view.name, provider=provider, model=model)
    command = _verbatim(f"git ls-remote {PUBLIC_REPO} HEAD")

    await agent.run(command)
    refused = await _decision_for(client, view.name, GITHUB_HOST)
    assert refused.outcome is Outcome.DENY, f"an unbound sandbox reached GitHub: {refused!r}"

    binding = await client.grant_egress(view.name, [GITHUB_PUBLIC])
    await _active(client, view.name, binding.name)

    turn = await agent.run(command)
    admitted = await _decision_for(client, view.name, GITHUB_HOST, after=refused.at)
    assert admitted.outcome is Outcome.ALLOW, f"{admitted!r}\n{turn.transcript}"


if __name__ == "__main__":
    pytest_bazel.main()
