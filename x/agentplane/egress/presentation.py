"""Where a credential's placeholder sits in a request, and how to put the real value there.

One decomposition per `TargetMethod`, and both halves of the decision read it: `present` finds a
credential's placeholder and hands back the rebuilders that same parse produced, so a placeholder
the proxy recognised cannot be one it forwards. A detector and a substituter that each decompose
the value their own way is the failure this module exists to foreclose.

A component must equal the placeholder exactly. A placeholder that is merely a substring of one is
not presented here, so it is neither detected nor substituted and reaches the upstream inert --
which is the point: it is not a secret, and a request that splices a real credential into the
middle of an unrelated string is the thing worth making impossible.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from x.agentplane.egress.resources import (
    BasicPasswordTarget,
    BasicUsernameTarget,
    BasicWholeTarget,
    EgressCredential,
    SchemeTokenTarget,
    Target,
    WholeValueTarget,
)

BASIC = "basic"


@dataclass(frozen=True)
class Parsed:
    """One header value decomposed by one target: the credential component it carries, and how to
    rebuild the value around a different one."""

    component: str
    rebuild: Callable[[str], str]


@dataclass(frozen=True)
class HeaderRewrite:
    """One header as it must be forwarded: all of its values, in order, with the real credential in
    every position that presented the placeholder and the rest untouched."""

    header: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class HeaderPresentation:
    header: str
    rebuilders: tuple[Callable[[str], str], ...] = ()

    def rewrite(self, credential: str) -> HeaderRewrite:
        return HeaderRewrite(header=self.header, values=tuple(rebuild(credential) for rebuild in self.rebuilders))


@dataclass(frozen=True)
class Presentation:
    """One credential as a request actually presents it: which credential, and for every header
    carrying its placeholder, how to rebuild that header's values around the real value."""

    credential: str
    headers: tuple[HeaderPresentation, ...]

    def rewrites(self, credential: str) -> tuple[HeaderRewrite, ...]:
        return tuple(header.rewrite(credential) for header in self.headers)


def parse(target: Target, value: str) -> Parsed | None:
    """The credential component `target` reads out of this header value, or None when the value is
    not of that target's shape."""
    match target:
        case WholeValueTarget():
            return Parsed(component=value, rebuild=lambda credential: credential)
        case SchemeTokenTarget():
            sent, separator, token = value.partition(" ")
            if not separator or sent.lower() != target.scheme.lower():
                return None
            # The scheme goes back as the client sent it: clients disagree on case, and git's own
            # documented form is a lowercase `bearer`.
            return Parsed(component=token, rebuild=lambda credential: f"{sent} {credential}")
        case BasicWholeTarget():
            if (basic := _basic_payload(value)) is None:
                return None
            scheme, payload = basic
            return Parsed(component=payload, rebuild=lambda credential: _basic_value(scheme, credential))
        case BasicUsernameTarget():
            if (pair := _basic_pair(value)) is None:
                return None
            scheme, username, password = pair
            return Parsed(
                component=username, rebuild=lambda credential: _basic_value(scheme, f"{credential}:{password}")
            )
        case BasicPasswordTarget():
            if (pair := _basic_pair(value)) is None:
                return None
            scheme, username, password = pair
            return Parsed(
                component=password, rebuild=lambda credential: _basic_value(scheme, f"{username}:{credential}")
            )


def present(credential: EgressCredential, headers: Mapping[str, Sequence[str]]) -> Presentation | None:
    """Where `credential`'s placeholder sits in these headers, or None when it is not presented."""
    placeholder = credential.placeholder
    values_by_name = {name.lower(): tuple(values) for name, values in headers.items()}
    presented: list[HeaderPresentation] = []
    for name, header in _headers_of(credential).items():
        rebuilders = []
        matched = False
        for value in values_by_name.get(name, ()):
            rebuild = _rebuild_of(credential, name, value, placeholder)
            matched = matched or rebuild is not None
            rebuilders.append(rebuild if rebuild is not None else _unchanged(value))
        if matched:
            presented.append(HeaderPresentation(header=header, rebuilders=tuple(rebuilders)))
    return Presentation(credential=credential.metadata.name, headers=tuple(presented)) if presented else None


def _headers_of(credential: EgressCredential) -> dict[str, str]:
    """The headers this credential's targets name, lowercase key to the spelling the policy declared
    -- which is the spelling the rewritten header goes out under, whatever case the client sent."""
    headers: dict[str, str] = {}
    for target in credential.spec.targets:
        headers.setdefault(target.header.lower(), target.header)
    return headers


def _rebuild_of(credential: EgressCredential, header: str, value: str, placeholder: str) -> Callable[[str], str] | None:
    """The rebuild of the first target on `header` whose component is exactly the placeholder.

    Which target that is never turns on order in practice: a value is of at most one target's shape,
    since a `Basic` payload is not a `Bearer` token and neither is the other.
    """
    for target in credential.spec.targets:
        if target.header.lower() != header:
            continue
        parsed = parse(target, value)
        if parsed is not None and parsed.component == placeholder:
            return parsed.rebuild
    return None


def _unchanged(value: str) -> Callable[[str], str]:
    return lambda _credential: value


def _basic_payload(value: str) -> tuple[str, str] | None:
    """The scheme as sent and the decoded payload, or None when the value is not a `Basic` one."""
    scheme, separator, encoded = value.partition(" ")
    if not separator or scheme.lower() != BASIC:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return scheme, payload


def _basic_pair(value: str) -> tuple[str, str, str] | None:
    """Scheme, username and password, or None when the value is not `Basic` or its payload carries
    no `:` to split on -- a payload without one is `basicWhole`'s case, not these two's."""
    if (basic := _basic_payload(value)) is None:
        return None
    scheme, payload = basic
    username, separator, password = payload.partition(":")
    return (scheme, username, password) if separator else None


def _basic_value(scheme: str, payload: str) -> str:
    return f"{scheme} {base64.b64encode(payload.encode()).decode()}"
