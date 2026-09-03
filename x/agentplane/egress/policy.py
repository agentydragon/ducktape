"""The decision: bindings to policies to the first matching rule, over an in-memory index. No I/O.

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
from typing import assert_never

from x.agentplane.egress.resources import (
    ACTIVE_CONDITION,
    ActiveReason,
    BindingStatus,
    Condition,
    ConditionStatus,
    Credential,
    EgressBinding,
    EgressPolicy,
    NamedSubject,
    Rule,
    Sandbox,
    Secret,
    SelectedSubjects,
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
    """The active bindings naming this Sandbox, by name or by label selector, in name order."""
    return [
        resolution
        for name in sorted(index.bindings)
        if (resolution := resolve_binding(index, index.bindings[name], now)).active
        and any(_subject_matches(subject, sandbox) for subject in resolution.binding.spec.subjects)
    ]


def _subject_matches(subject: NamedSubject | SelectedSubjects, sandbox: Sandbox) -> bool:
    match subject:
        case NamedSubject():
            return subject.sandbox.name == sandbox.metadata.name
        case SelectedSubjects():
            labels = sandbox.metadata.labels
            return all(labels.get(key) == value for key, value in subject.sandbox_selector.match_labels.items())
        case _:
            assert_never(subject)


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


def evaluate(index: Index, sandbox: Sandbox, request: EgressRequest, now: datetime) -> Decision:
    """Fail closed: the first rule matching host, method and path decides; nothing matching denies.

    A matched credential rule swaps the placeholder in its header for the Secret value. Whatever
    the outcome, a known placeholder still present in its header after substitution denies the
    request: a placeholder is never forwarded.
    """
    bindings = subject_bindings(index, sandbox, now)
    if not bindings:
        return Denied(DenyReason.NO_BINDING)
    for resolution in bindings:
        for policy in resolution.policies:
            for number, rule in enumerate(policy.spec.rules):
                if not rule_matches(rule, request):
                    continue
                if request.is_connect or rule.credential is None:
                    substitution = None
                else:
                    secret = index.secrets.get(rule.credential.secret_ref.name)
                    value = secret.data.get(rule.credential.secret_ref.key) if secret is not None else None
                    if value is None:
                        return Denied(DenyReason.CREDENTIAL_UNAVAILABLE)
                    substitution = _substitute(request, rule.credential, value)
                if not request.is_connect and _placeholder_left(index, request, substitution):
                    return Denied(DenyReason.PLACEHOLDER_UNRESOLVED)
                return Allowed(
                    binding=resolution.binding.metadata.name,
                    policy=policy.metadata.name,
                    rule=number,
                    substitution=substitution,
                )
    return Denied(DenyReason.NO_RULE)


def _header_values(request: EgressRequest, header: str) -> tuple[str, ...]:
    header = header.lower()
    return tuple(value for name, values in request.headers.items() if name.lower() == header for value in values)


def _substitute(request: EgressRequest, credential: Credential, value: str) -> Substitution | None:
    values = _header_values(request, credential.header)
    swapped = tuple(swap_placeholder(current, credential.placeholder, value) for current in values)
    return Substitution(header=credential.header, values=swapped) if swapped != values else None


def _placeholder_left(index: Index, request: EgressRequest, substitution: Substitution | None) -> bool:
    for policy in index.policies.values():
        for rule in policy.spec.rules:
            if rule.credential is None:
                continue
            values = (
                substitution.values
                if substitution is not None and substitution.header.lower() == rule.credential.header.lower()
                else _header_values(request, rule.credential.header)
            )
            if any(contains_placeholder(value, rule.credential.placeholder) for value in values):
                return True
    return False


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
