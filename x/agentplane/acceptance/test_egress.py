"""What a sandbox may reach, checked against a deployed Agentplane by running real agents in it.

Each scenario asks a harness to make a request and then reads the proxy's decision ring through the
app. The ring is what is asserted on because it is the system's own record of what happened: an
agent's account of its own tool call is prose, and prose saying "I fetched it" proves nothing about
which credential went on the wire or whether the request was admitted.

E3's acceptance in x/agentplane/plans/egress_proxy.md is what these encode. They exist because the
last gap of this kind -- a runner that dropped the proxy variables, so every call bypassed the proxy
and hung, leaving an empty ring behind a green unit suite -- was caught by a person noticing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

import pytest_bazel
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from x.agentplane.acceptance.agent import Agent
from x.agentplane.acceptance.client import Client
from x.agentplane.app.decisions import Decision, Outcome
from x.agentplane.app.inventory import SandboxView

# The policy staging seeds: GitHub's API and HTTPS git for public repositories, with a PAT the proxy
# substitutes (cluster/k8s/agentplane-staging/egress/egresspolicy-github-public.yaml).
GITHUB_PUBLIC = "github-public"
GITHUB_HOST = "github.com"
PUBLIC_REPO = "https://github.com/agentydragon/ducktape"
# Named by no policy staging has, so it is refused for want of a rule rather than by one.
UNLISTED_HOST = "example.com"

CLAUDE = "PROVIDER_CLAUDE"
HAIKU = "anthropic-api/ant-messages/claude-haiku-4-5-20251001"

# The proxy records a decision as it serves it; the app reads the ring over a separate hop, and the
# binding's Active condition is written by the proxy's informer rather than by the grant itself.
DECISION_SECONDS = 30.0
BINDING_SECONDS = 60.0

Sandboxes = Callable[..., Awaitable[SandboxView]]


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


async def test_a_host_its_policy_names_is_reached_through_the_proxy(client: Client, sandbox: Sandboxes) -> None:
    """The E3 acceptance: a public-repo git call from inside a sandbox succeeds, and the proxy is what
    served it. A sandbox dialling the internet directly hangs instead of being admitted, so this
    fails rather than passing quietly."""
    view = await sandbox("accept-allow", policies=[GITHUB_PUBLIC])
    agent = await Agent.open(client, sandbox=view.name, provider=CLAUDE, model=HAIKU)
    turn = await agent.run(f"Run exactly this command and report its output verbatim: git ls-remote {PUBLIC_REPO} HEAD")

    decision = await _decision_for(client, view.name, GITHUB_HOST)
    assert decision.outcome is Outcome.ALLOW, f"{decision!r}\n{turn.transcript}"
    assert decision.policy == GITHUB_PUBLIC, f"{decision!r}"
    assert any("HEAD" in output for output in turn.tool_outputs), (
        f"the proxy admitted the call but the agent saw no refs:\n{turn.transcript}"
    )


async def test_a_host_no_policy_names_is_refused(client: Client, sandbox: Sandboxes) -> None:
    """Fail closed, and fail fast: the refusal is the proxy's answer, so it arrives rather than the
    request timing out somewhere with nothing recorded."""
    view = await sandbox("accept-deny", policies=[GITHUB_PUBLIC])
    agent = await Agent.open(client, sandbox=view.name, provider=CLAUDE, model=HAIKU)
    turn = await agent.run(
        "Run exactly this command and report its output verbatim: "
        f"curl -sS -o /dev/null -w '%{{http_code}}' https://{UNLISTED_HOST}"
    )

    decision = await _decision_for(client, view.name, UNLISTED_HOST)
    assert decision.outcome is Outcome.DENY, f"{decision!r}\n{turn.transcript}"
    assert decision.reason == "no-rule", f"{decision!r}"


async def test_the_substituted_credential_never_enters_the_sandbox(client: Client, sandbox: Sandboxes) -> None:
    """The point of the design: the sandbox reaches GitHub with a credential it cannot itself read."""
    view = await sandbox("accept-secret", policies=[GITHUB_PUBLIC])
    agent = await Agent.open(client, sandbox=view.name, provider=CLAUDE, model=HAIKU)
    turn = await agent.run(
        "Run exactly this command and report its output verbatim: env | grep -Ei 'github|_pat_|ghp_' ; echo \"rc=$?\""
    )

    assert not any("ghp_" in output or "github_pat_" in output for output in turn.tool_outputs), (
        f"a GitHub credential is readable inside the sandbox:\n{turn.transcript}"
    )


async def test_a_policy_granted_after_the_sandbox_is_running_takes_effect(client: Client, sandbox: Sandboxes) -> None:
    """Binding at runtime is a live grant, not a restart: one sandbox, refused and then admitted."""
    view = await sandbox("accept-bind")
    agent = await Agent.open(client, sandbox=view.name, provider=CLAUDE, model=HAIKU)
    command = f"Run exactly this command and report its output verbatim: git ls-remote {PUBLIC_REPO} HEAD"

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
