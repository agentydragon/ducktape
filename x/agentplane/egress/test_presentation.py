"""The parse table: what each method reads out of a header value, and what it puts back.

The pair that matters is exactness and rebuilding. A component must equal the placeholder entirely,
so a value that merely contains it is not presented; and every rebuild is the inverse of the parse
that found it, so a value the proxy recognised is a value it can put the real credential into.
"""

from __future__ import annotations

import base64

import pytest
import pytest_bazel

from x.agentplane.egress.presentation import HeaderRewrite, parse, present
from x.agentplane.egress.resources import (
    BasicPasswordTarget,
    BasicUsernameTarget,
    BasicWholeTarget,
    CredentialSource,
    CredentialSpec,
    EgressCredential,
    ObjectMeta,
    SchemeTokenTarget,
    SecretKeyRef,
    Target,
    TargetMethod,
    WholeValueTarget,
    placeholder_of,
)

AUTHORIZATION = "Authorization"
CREDENTIAL = "the-real-value"
BEARER = SchemeTokenTarget(header=AUTHORIZATION, method=TargetMethod.SCHEME_TOKEN, scheme="Bearer")
WHOLE = WholeValueTarget(header="X-Api-Key", method=TargetMethod.WHOLE_VALUE)
BASIC_USERNAME = BasicUsernameTarget(header=AUTHORIZATION, method=TargetMethod.BASIC_USERNAME)
BASIC_PASSWORD = BasicPasswordTarget(header=AUTHORIZATION, method=TargetMethod.BASIC_PASSWORD)
BASIC_WHOLE = BasicWholeTarget(header=AUTHORIZATION, method=TargetMethod.BASIC_WHOLE)


def basic(payload: str) -> str:
    return f"Basic {base64.b64encode(payload.encode()).decode()}"


def credential(name: str, *targets: Target) -> EgressCredential:
    return EgressCredential(
        metadata=ObjectMeta(name=name),
        spec=CredentialSpec(
            source=CredentialSource(secret_ref=SecretKeyRef(name="pat", key="token")),
            description=f"the {name} test credential",
            targets=list(targets),
        ),
    )


PLACEHOLDER = placeholder_of("github-pat")


@pytest.mark.parametrize(
    ("target", "value", "component"),
    [
        (WHOLE, "abc", "abc"),
        (BEARER, "Bearer abc", "abc"),
        (BEARER, "bearer abc", "abc"),
        (BASIC_USERNAME, basic("abc:secret"), "abc"),
        (BASIC_PASSWORD, basic("user:abc"), "abc"),
        (BASIC_PASSWORD, basic("user:with:colons"), "with:colons"),
        (BASIC_WHOLE, basic("abc"), "abc"),
        (BASIC_WHOLE, basic("a:b"), "a:b"),
    ],
)
def test_a_parse_reads_its_component_and_puts_a_new_one_back(target: Target, value: str, component: str) -> None:
    parsed = parse(target, value)

    assert parsed is not None
    assert parsed.component == component
    rebuilt = parsed.rebuild(CREDENTIAL)
    assert rebuilt != value
    reparsed = parse(target, rebuilt)
    assert reparsed is not None
    assert reparsed.component == CREDENTIAL, "the rebuild is not the inverse of the parse that found it"


@pytest.mark.parametrize(
    ("target", "value"),
    [
        (BEARER, "abc"),
        (BEARER, "Token abc"),
        (BEARER, "Basic " + base64.b64encode(b"a:b").decode()),
        (BASIC_USERNAME, "Bearer abc"),
        (BASIC_USERNAME, basic("nocolon")),
        (BASIC_PASSWORD, basic("nocolon")),
        (BASIC_WHOLE, "Basic not-base64!"),
        (BASIC_WHOLE, "Basic " + base64.b64encode(b"\xff\xfe").decode()),
    ],
)
def test_a_value_of_another_shape_is_not_this_target_s(target: Target, value: str) -> None:
    assert parse(target, value) is None


@pytest.mark.parametrize(
    "value", [f"Bearer prefix{PLACEHOLDER}", f"Bearer {PLACEHOLDER}suffix", f"Bearer {PLACEHOLDER} extra", PLACEHOLDER]
)
def test_a_component_that_only_contains_the_placeholder_is_not_presented(value: str) -> None:
    """The substring replace this design replaces would have spliced the real credential into every
    one of these and forwarded it."""
    assert present(credential("github-pat", BEARER), {AUTHORIZATION: [value]}) is None


def test_the_placeholder_is_found_at_whichever_declared_target_carries_it() -> None:
    github = credential("github-pat", BEARER, BASIC_PASSWORD)

    bearer = present(github, {AUTHORIZATION: [f"Bearer {PLACEHOLDER}"]})
    assert bearer is not None
    assert bearer.rewrites(CREDENTIAL) == (HeaderRewrite(AUTHORIZATION, (f"Bearer {CREDENTIAL}",)),)

    git = present(github, {"authorization": [basic(f"x-access-token:{PLACEHOLDER}")]})
    assert git is not None
    assert git.rewrites(CREDENTIAL) == (HeaderRewrite(AUTHORIZATION, (basic(f"x-access-token:{CREDENTIAL}"),)),)


def test_a_header_no_target_names_is_not_looked_at() -> None:
    assert present(credential("github-pat", BEARER), {"X-Other": [f"Bearer {PLACEHOLDER}"]}) is None


def test_every_value_of_a_repeated_header_is_decided_on_its_own() -> None:
    """A header sent twice is parsed per value: the one presenting the placeholder is rewritten and
    the other is forwarded exactly as it came."""
    presentation = present(credential("github-pat", BEARER), {AUTHORIZATION: ["Bearer other", f"Bearer {PLACEHOLDER}"]})

    assert presentation is not None
    assert presentation.rewrites(CREDENTIAL) == (
        HeaderRewrite(AUTHORIZATION, ("Bearer other", f"Bearer {CREDENTIAL}")),
    )


def test_a_credential_presented_in_two_headers_is_rewritten_in_both() -> None:
    two = credential("github-pat", BEARER, WHOLE)
    presentation = present(two, {AUTHORIZATION: [f"Bearer {PLACEHOLDER}"], "x-api-key": [PLACEHOLDER]})

    assert presentation is not None
    assert presentation.rewrites(CREDENTIAL) == (
        HeaderRewrite(AUTHORIZATION, (f"Bearer {CREDENTIAL}",)),
        HeaderRewrite(WHOLE.header, (CREDENTIAL,)),
    )


def test_the_rewritten_header_carries_the_spelling_the_policy_declared() -> None:
    """Whatever case the client sent, so the forwarded request reads as the credential declares it."""
    presentation = present(credential("github-pat", BEARER), {"AUTHORIZATION": [f"Bearer {PLACEHOLDER}"]})

    assert presentation is not None
    assert presentation.rewrites(CREDENTIAL)[0].header == AUTHORIZATION


if __name__ == "__main__":
    pytest_bazel.main()
