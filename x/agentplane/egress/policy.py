"""The decision: bindings to policies to the rule the request's placeholder picks, over an in-memory index. No I/O.

`Index` is the proxy's picture of the namespace, kept equal to the API server's by the informer;
`evaluate` answers one request for one subject against it, and `binding_status` derives the status
the proxy writes back. Both take `now` so expiry is decided by the caller's clock.

Where a credential sits in a request, and what the forwarded headers become, is `presentation.py`:
one parse per declared target, read by detection and substitution alike.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from more_itertools import one

from x.agentplane.egress.presentation import HeaderRewrite, Presentation, present
from x.agentplane.egress.resources import (
    ACTIVE_CONDITION,
    ActiveReason,
    BindingStatus,
    Condition,
    ConditionStatus,
    EgressBinding,
    EgressCredential,
    EgressPolicy,
    Rule,
    Sandbox,
    Secret,
)

CONNECT = "CONNECT"


class DenyReason(StrEnum):
    """The machine-readable half of every refusal, as the `x-agentplane-egress` header carries it."""

    TOKEN_MISSING = "token-missing"
    TOKEN_REJECTED = "token-rejected"
    POD_MISMATCH = "pod-mismatch"
    SANDBOX_UNKNOWN = "sandbox-unknown"
    NO_BINDING = "no-binding"
    NO_RULE = "no-rule"
    PLACEHOLDER_UNRESOLVED = "placeholder-unresolved"
    CREDENTIAL_UNAVAILABLE = "credential-unavailable"
    ADDRESS_FORBIDDEN = "address-forbidden"
    HOST_UNRESOLVED = "host-unresolved"
    UNAVAILABLE = "unavailable"


@dataclass
class Index:
    """Everything the decision reads, keyed by name. Mutated only by the informer; `changed` pulses
    on every mutation so readers can wait for a state rather than a duration."""

    policies: dict[str, EgressPolicy] = field(default_factory=dict)
    bindings: dict[str, EgressBinding] = field(default_factory=dict)
    credentials: dict[str, EgressCredential] = field(default_factory=dict)
    sandboxes: dict[str, Sandbox] = field(default_factory=dict)
    secrets: dict[str, Secret] = field(default_factory=dict, repr=False)
    synced: bool = field(default=False)
    # When each watched kind last completed a full list-and-watch cycle, keyed by plural. A cycle
    # ends when the API server closes the watch at `resync_seconds`, so under health every entry
    # advances that often. One that stops is the failure this exists to expose: a wedged list, a
    # watch that never returns, or one the server keeps refusing leaves the index frozen while
    # every answer it gives stays plausible and `synced` stays true.
    refreshed: dict[str, datetime] = field(default_factory=dict)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with self.changed:
            await self.changed.wait_for(predicate)


@dataclass(frozen=True)
class EgressRequest:
    """One admission as the proxy sees it: the connection target, never the Host header."""

    method: str
    host: str
    port: int
    path: str | None = field(default=None)
    headers: Mapping[str, Sequence[str]] = field(default_factory=dict)

    @property
    def is_connect(self) -> bool:
        return self.method == CONNECT


@dataclass(frozen=True)
class Allowed:
    binding: str
    policy: str
    rule: int
    rewrites: tuple[HeaderRewrite, ...] = ()
    cluster_internal: bool = False
    """Whether the deciding rule declared its hosts cluster-internal, which the dial needs: the
    address check happens where the connection is made, not here."""


@dataclass(frozen=True)
class Denied:
    reason: DenyReason


type Decision = Allowed | Denied


@dataclass(frozen=True)
class BindingResolution:
    """A binding as it stands right now: which of its policies exist, and why it may grant nothing."""

    binding: EgressBinding
    policies: tuple[EgressPolicy, ...]
    missing: tuple[str, ...]
    reason: ActiveReason

    @property
    def active(self) -> bool:
        return self.reason is ActiveReason.RESOLVED


def resolve_binding(index: Index, binding: EgressBinding, now: datetime) -> BindingResolution:
    policies = tuple(index.policies[name] for name in binding.spec.policies if name in index.policies)
    missing = tuple(name for name in binding.spec.policies if name not in index.policies)
    spec = binding.spec
    if spec.expires_at is not None and spec.expires_at <= now:
        reason = ActiveReason.EXPIRED
    elif not policies:
        reason = ActiveReason.MISSING_POLICY
    else:
        reason = ActiveReason.RESOLVED
    return BindingResolution(binding=binding, policies=policies, missing=missing, reason=reason)


def binding_status(index: Index, binding: EgressBinding, now: datetime) -> BindingStatus:
    """The status the proxy writes: `observedGeneration`, the `Active` condition, `resolvedPolicies`.

    The transition time carries over from the current condition when the status bit is unchanged,
    so a status computed twice compares equal and nothing is written twice.
    """
    resolution = resolve_binding(index, binding, now)
    status = ConditionStatus.TRUE if resolution.active else ConditionStatus.FALSE
    previous = next(
        (condition for condition in (binding.status.conditions if binding.status else []) if _is_active(condition)),
        None,
    )
    transition = previous.last_transition_time if previous is not None and previous.status is status else now
    message = f"{len(resolution.policies)} of {len(binding.spec.policies)} policies resolved"
    if resolution.missing:
        message += f"; missing: {', '.join(resolution.missing)}"
    return BindingStatus(
        observed_generation=binding.metadata.generation,
        conditions=[
            Condition(
                type=ACTIVE_CONDITION,
                status=status,
                reason=resolution.reason,
                message=message,
                last_transition_time=transition,
                observed_generation=binding.metadata.generation,
            )
        ],
        resolved_policies=len(resolution.policies),
    )


def _is_active(condition: Condition) -> bool:
    return condition.type == ACTIVE_CONDITION


def subject_bindings(index: Index, sandbox: Sandbox, now: datetime) -> list[BindingResolution]:
    """The active bindings naming this Sandbox, in name order."""
    return [
        resolution
        for name in sorted(index.bindings)
        if (resolution := resolve_binding(index, index.bindings[name], now)).active
        and any(subject.sandbox.name == sandbox.metadata.name for subject in resolution.binding.spec.subjects)
    ]


def host_matches(pattern: str, host: str) -> bool:
    """Exact, case-insensitive; `*.example.com` matches any subdomain depth but not the apex."""
    host = host.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and len(host) > len(pattern) - 1
    return host == pattern


def path_matches(pattern: str, path: str) -> bool:
    """Glob over the path without its query: `*` stays within a segment, `**` crosses segments."""
    regex = "".join(
        ".*" if token == "**" else "[^/]*" if token == "*" else re.escape(token)
        for token in re.split(r"(\*\*|\*)", pattern)
        if token
    )
    return re.fullmatch(regex, path.partition("?")[0]) is not None


def rule_matches(rule: Rule, request: EgressRequest) -> bool:
    """A CONNECT is admitted on host alone; the requests inside the tunnel are decided one by one."""
    if not any(host_matches(pattern, request.host) for pattern in rule.hosts):
        return False
    if request.is_connect:
        return True
    if rule.methods is not None and request.method.upper() not in {method.upper() for method in rule.methods}:
        return False
    return rule.paths is None or any(path_matches(pattern, request.path or "/") for pattern in rule.paths)


@dataclass(frozen=True)
class _Match:
    """A rule that matches the request, and where the subject got it."""

    binding: str
    policy: str
    number: int
    rule: Rule


def _matching_rules(bindings: Sequence[BindingResolution], request: EgressRequest) -> list[_Match]:
    """Every rule that admits the request, in walk order: bindings by name, policies and rules as listed."""
    return [
        _Match(binding=resolution.binding.metadata.name, policy=policy.metadata.name, number=number, rule=rule)
        for resolution in bindings
        for policy in resolution.policies
        for number, rule in enumerate(policy.spec.rules)
        if rule_matches(rule, request)
    ]


def presented_credentials(index: Index, request: EgressRequest) -> dict[str, Presentation]:
    """Every known credential this request presents, by name.

    Known is namespace-wide: any `EgressCredential` in the index counts, whether or not the subject
    is bound to a policy naming it, so a placeholder the subject was never granted is recognised and
    refused rather than forwarded.
    """
    return {
        credential.metadata.name: presentation
        for credential in index.credentials.values()
        if (presentation := present(credential, request.headers)) is not None
    }


def _resolves(rule: Rule, presented: Collection[str]) -> bool:
    """Whether this rule names exactly the one credential the request presents."""
    return rule.credential_ref is not None and set(presented) == {rule.credential_ref.name}


def evaluate(index: Index, sandbox: Sandbox, request: EgressRequest, now: datetime) -> Decision:
    """Fail closed: only a matching rule admits, and the placeholder the request presents picks which.

    A request presenting a known placeholder is decided by a matching rule naming exactly that
    credential; one presenting none is decided by the first matching rule and is forwarded as it
    came. So a placeholder is never forwarded, and widening what a subject may reach never takes a
    credential away from it.
    """
    bindings = subject_bindings(index, sandbox, now)
    if not bindings:
        return Denied(DenyReason.NO_BINDING)
    matches = _matching_rules(bindings, request)
    if not matches:
        return Denied(DenyReason.NO_RULE)
    # A CONNECT is decided on host alone: its headers belong to the tunnel, and the requests inside
    # it are decided one by one, which is where a target applies.
    presented = {} if request.is_connect else presented_credentials(index, request)
    if not presented:
        first = matches[0]
        return Allowed(
            binding=first.binding, policy=first.policy, rule=first.number, cluster_internal=first.rule.cluster_internal
        )
    resolving = [match for match in matches if _resolves(match.rule, presented)]
    if not resolving:
        return Denied(DenyReason.PLACEHOLDER_UNRESOLVED)
    match = resolving[0]
    # `_resolves` admitted exactly one presented credential, and every presented one is in the index.
    credential = index.credentials[one(presented)]
    secret_ref = credential.spec.source.secret_ref
    secret = index.secrets.get(secret_ref.name)
    value = secret.data.get(secret_ref.key) if secret is not None else None
    if value is None:
        return Denied(DenyReason.CREDENTIAL_UNAVAILABLE)
    return Allowed(
        binding=match.binding,
        policy=match.policy,
        rule=match.number,
        rewrites=presented[credential.metadata.name].rewrites(value),
        cluster_internal=match.rule.cluster_internal,
    )
