"""What a sandbox may know about its own egress.

An agent cannot act on rules it cannot read. Without this it has to be told out of band which hosts
it may reach and how to present a credential it does not hold -- and being told out of band means a
prompt, a hardcoded constant in a test, or a guess.

What a sandbox is told about a credential is its placeholder and every target it may be presented
in: the header, the shape of the value, and the scheme where the shape has one. That is exactly
enough to build a request the proxy will substitute into -- a placeholder plus a header name is not,
because a client has to know whether the value reads `Bearer <placeholder>` or the placeholder bare.

It is also told the credential's `description`, which is how it learns *whose* credential it is
about to spend and what that identity can do. An agent holding an opaque placeholder can send a
request; only one that knows the placeholder stands for a bot account's token can weigh whether to.

Secretless by construction, and by construction rather than by discipline: this module builds the
projection from its own field list and never from a resource object wholesale, so a field added to
`EgressCredential` -- a second source, a decrypted value, anything -- does not appear here until
someone writes it in. The value source never does.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from x.agentplane.egress.policy import Index, subject_bindings
from x.agentplane.egress.resources import EgressCredential, Rule, Sandbox, SchemeTokenTarget, Target, TargetMethod


class TargetView(BaseModel):
    """One place the credential may be presented, as the client has to build it."""

    model_config = ConfigDict(extra="forbid")

    header: str = Field(description="Request header this presentation puts the placeholder in.")
    method: TargetMethod = Field(description="Shape of the header value around the placeholder.")
    scheme: str | None = Field(
        default=None, description="The scheme `schemeToken` expects; absent for every other method."
    )


class CredentialView(BaseModel):
    """How to present a credential the sandbox does not hold: what to send, and where."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(description="What the credential is and what it can do, as its owner wrote it.")
    placeholder: str = Field(description="Inert string to send; the proxy swaps the real value in.")
    targets: list[TargetView] = Field(description="Every location the proxy substitutes at.")


class RuleView(BaseModel):
    """One rule as the sandbox may see it."""

    model_config = ConfigDict(extra="forbid")

    hosts: list[str]
    methods: list[str] | None = Field(default=None, description="Absent admits any method.")
    paths: list[str] | None = Field(default=None, description="Absent admits any path.")
    credential: CredentialView | None = Field(
        default=None, description="Present when the proxy substitutes one for this rule."
    )


class PolicyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rules: list[RuleView]


class AgentEgressView(BaseModel):
    """Everything the sandbox may reach, as the proxy would decide it right now."""

    model_config = ConfigDict(extra="forbid")

    sandbox: str
    policies: list[PolicyView] = Field(description="Granted by an active binding; empty means no egress.")


def _target_view(target: Target) -> TargetView:
    return TargetView(
        header=target.header,
        method=target.method,
        scheme=target.scheme if isinstance(target, SchemeTokenTarget) else None,
    )


def _credential_view(credential: EgressCredential) -> CredentialView:
    return CredentialView(
        name=credential.metadata.name,
        description=credential.spec.description,
        placeholder=credential.placeholder,
        targets=[_target_view(target) for target in credential.spec.targets],
    )


def _rule_view(index: Index, rule: Rule) -> RuleView:
    # A rule naming a credential the namespace does not hold reads as no credential, which is what
    # the decision does with it too: nothing to present, so nothing to substitute.
    credential = None if rule.credential_ref is None else index.credentials.get(rule.credential_ref.name)
    return RuleView(
        hosts=list(rule.hosts),
        methods=list(rule.methods) if rule.methods is not None else None,
        paths=list(rule.paths) if rule.paths is not None else None,
        credential=None if credential is None else _credential_view(credential),
    )


def agent_view(index: Index, sandbox: Sandbox, now: datetime) -> AgentEgressView:
    """The sandbox's own view, from the same bindings the decision reads.

    Built from `subject_bindings`, so what it reports and what the proxy admits cannot drift: an
    expired binding, a missing policy, or a revoked grant drops out of both at once.
    """
    return AgentEgressView(
        sandbox=sandbox.metadata.name,
        policies=[
            PolicyView(name=policy.metadata.name, rules=[_rule_view(index, rule) for rule in policy.spec.rules])
            for resolution in subject_bindings(index, sandbox, now)
            for policy in resolution.policies
        ],
    )
