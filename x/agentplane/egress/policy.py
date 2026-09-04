"""The decision: bindings to policies to the rule the request's placeholder picks, over an in-memory index. No I/O.

`Index` is the proxy's picture of the namespace, kept equal to the API server's by the informer;
`evaluate` answers one request for one subject against it, and `binding_status` derives the status
the proxy writes back. Both take `now` so expiry is decided by the caller's clock.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from x.agentplane.egress.resources import (
    ACTIVE_CONDITION,
    ActiveReason,
    BindingStatus,
    Condition,
    ConditionStatus,
    Credential,
    EgressBinding,
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
class Substitution:
    """The one header to rewrite before forwarding, already rewritten."""

    header: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class Allowed:
    binding: str
    policy: str
    rule: int
    substitution: Substitution | None = None


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


@dataclass(frozen=True)
class _Placeholder:
    """What a request presents and a credential resolves: the string, and the header it sits in."""

    header: str
    text: str

    @classmethod
    def of(cls, credential: Credential) -> _Placeholder:
        return cls(header=credential.header.lower(), text=credential.placeholder)


def _matching_rules(bindings: Sequence[BindingResolution], request: EgressRequest) -> list[_Match]:
    """Every rule that admits the request, in walk order: bindings by name, policies and rules as listed."""
    return [
        _Match(binding=resolution.binding.metadata.name, policy=policy.metadata.name, number=number, rule=rule)
        for resolution in bindings
        for policy in resolution.policies
        for number, rule in enumerate(policy.spec.rules)
        if rule_matches(rule, request)
    ]


def _presented_placeholders(index: Index, request: EgressRequest) -> set[_Placeholder]:
    """The known placeholders the request carries.

    Known is namespace-wide: any credential of any `EgressPolicy` names one, whether or not the
    subject is bound to that policy, so a placeholder it was never granted is recognised too.
    """
    presented: set[_Placeholder] = set()
    for policy in index.policies.values():
        for rule in policy.spec.rules:
            credential = rule.credential
            if credential is not None and any(
                contains_placeholder(value, credential.placeholder)
                for value in _header_values(request, credential.header)
            ):
                presented.add(_Placeholder.of(credential))
    return presented


def _resolves(rule: Rule, presented: set[_Placeholder]) -> bool:
    """Whether this rule's credential substitutes away every placeholder the request carries."""
    return rule.credential is not None and presented == {_Placeholder.of(rule.credential)}


def evaluate(index: Index, sandbox: Sandbox, request: EgressRequest, now: datetime) -> Decision:
    """Fail closed: only a matching rule admits, and the placeholder the request carries picks which.

    A request carrying a known placeholder is decided by a matching rule whose credential resolves
    exactly it; one carrying none is decided by the first matching rule. So a placeholder is never
    forwarded, and widening what a subject may reach never takes a credential away from it.
    """
    bindings = subject_bindings(index, sandbox, now)
    if not bindings:
        return Denied(DenyReason.NO_BINDING)
    matches = _matching_rules(bindings, request)
    if not matches:
        return Denied(DenyReason.NO_RULE)
    if not request.is_connect and (presented := _presented_placeholders(index, request)):
        matches = [match for match in matches if _resolves(match.rule, presented)]
        if not matches:
            return Denied(DenyReason.PLACEHOLDER_UNRESOLVED)
    match = matches[0]
    credential = None if request.is_connect else match.rule.credential
    if credential is None:
        return Allowed(binding=match.binding, policy=match.policy, rule=match.number)
    secret = index.secrets.get(credential.secret_ref.name)
    value = secret.data.get(credential.secret_ref.key) if secret is not None else None
    if value is None:
        return Denied(DenyReason.CREDENTIAL_UNAVAILABLE)
    return Allowed(
        binding=match.binding,
        policy=match.policy,
        rule=match.number,
        substitution=_substitute(request, credential, value),
    )


def _header_values(request: EgressRequest, header: str) -> tuple[str, ...]:
    header = header.lower()
    return tuple(value for name, values in request.headers.items() if name.lower() == header for value in values)


def _substitute(request: EgressRequest, credential: Credential, value: str) -> Substitution | None:
    values = _header_values(request, credential.header)
    swapped = tuple(swap_placeholder(current, credential.placeholder, value) for current in values)
    return Substitution(header=credential.header, values=swapped) if swapped != values else None


def _basic_payload(header_value: str) -> bytes | None:
    """The decoded `Basic` credential, the shape git over HTTPS sends; None for anything else."""
    scheme, separator, payload = header_value.partition(" ")
    if not separator or scheme.lower() != "basic":
        return None
    try:
        return base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None


def swap_placeholder(header_value: str, placeholder: str, value: str) -> str:
    """Substring swap, reaching inside a base64 `Basic` payload."""
    swapped = header_value.replace(placeholder, value)
    if swapped != header_value:
        return swapped
    payload = _basic_payload(header_value)
    if payload is None:
        return header_value
    swapped_payload = payload.replace(placeholder.encode(), value.encode())
    if swapped_payload == payload:
        return header_value
    return f"{header_value.partition(' ')[0]} {base64.b64encode(swapped_payload).decode()}"


def contains_placeholder(header_value: str, placeholder: str) -> bool:
    if placeholder in header_value:
        return True
    payload = _basic_payload(header_value)
    return payload is not None and placeholder.encode() in payload
