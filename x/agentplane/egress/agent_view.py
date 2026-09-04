"""What a sandbox may know about its own egress.

An agent cannot act on rules it cannot read. Without this it has to be told out of band which hosts
it may reach and which placeholder stands in for a credential -- and being told out of band means a
prompt, a hardcoded constant in a test, or a guess.

Secretless by construction, and by construction rather than by discipline: this module builds the
projection from its own field list and never from a rule object wholesale, so a field added to
`Credential` -- a second `secretRef`, a decrypted value, anything -- does not appear here until
someone writes it in. What a sandbox is told about a credential is the header it goes in and the
placeholder to put there, which is exactly what it needs to send a request and nothing that would
help it forge one.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from x.agentplane.egress.policy import Index, subject_bindings
from x.agentplane.egress.resources import Rule, Sandbox


class CredentialView(BaseModel):
    """How to present a credential the sandbox does not hold: the header, and what to put in it."""

    model_config = ConfigDict(extra="forbid")

    header: str = Field(description="Request header the placeholder goes in.")
    placeholder: str = Field(description="Inert string to send; the proxy swaps the real value in.")


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


def _rule_view(rule: Rule) -> RuleView:
    credential = rule.credential
    return RuleView(
        hosts=list(rule.hosts),
        methods=list(rule.methods) if rule.methods is not None else None,
        paths=list(rule.paths) if rule.paths is not None else None,
        credential=(
            None if credential is None else CredentialView(header=credential.header, placeholder=credential.placeholder)
        ),
    )


def agent_view(index: Index, sandbox: Sandbox, now: datetime) -> AgentEgressView:
    """The sandbox's own view, from the same bindings the decision reads.

    Built from `subject_bindings`, so what it reports and what the proxy admits cannot drift: an
    expired binding, a missing policy, or a revoked grant drops out of both at once.
    """
    return AgentEgressView(
        sandbox=sandbox.metadata.name,
        policies=[
            PolicyView(name=policy.metadata.name, rules=[_rule_view(rule) for rule in policy.spec.rules])
            for resolution in subject_bindings(index, sandbox, now)
            for policy in resolution.policies
        ],
    )
