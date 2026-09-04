"""The projection a sandbox may read of its own egress: what it says, and what it can never say."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_bazel

from x.agentplane.egress.agent_view import CredentialView, agent_view
from x.agentplane.egress.policy import Index
from x.agentplane.egress.resources import (
    BindingSpec,
    Credential,
    EgressBinding,
    EgressPolicy,
    ObjectMeta,
    PolicySpec,
    Rule,
    Sandbox,
    SandboxRef,
    Secret,
    SecretKeyRef,
    Subject,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
PLACEHOLDER = "PLACEHOLDER-TOKEN"
SECRET_VALUE = "the-real-credential"
SANDBOX = Sandbox(metadata=ObjectMeta(name="sb", uid="sb-uid"))
CREDENTIAL = Credential(
    secret_ref=SecretKeyRef(name="vault-entry", key="credential-key"), header="Authorization", placeholder=PLACEHOLDER
)
GITHUB_RULE = Rule(hosts=["api.github.com"], methods=["GET", "POST"], paths=["/repos/**"], credential=CREDENTIAL)
OPEN_RULE = Rule(hosts=["*.example.com"])


def _index(*, expires_at: datetime | None = None, policies: list[str] | None = None) -> Index:
    policy = EgressPolicy(
        metadata=ObjectMeta(name="github", generation=1), spec=PolicySpec(rules=[GITHUB_RULE, OPEN_RULE])
    )
    bound = EgressBinding(
        metadata=ObjectMeta(name="b", generation=1),
        spec=BindingSpec(
            subjects=[Subject(sandbox=SandboxRef(name="sb"))],
            policies=policies if policies is not None else ["github"],
            expires_at=expires_at,
        ),
    )
    return Index(
        policies={"github": policy},
        bindings={"b": bound},
        sandboxes={"sb": SANDBOX},
        secrets={"vault-entry": Secret(name="vault-entry", data={"credential-key": SECRET_VALUE})},
    )


def test_a_sandbox_is_told_the_header_and_the_placeholder() -> None:
    """Exactly what it needs to send an admitted request, and it cannot get this anywhere else."""
    view = agent_view(_index(), SANDBOX, NOW)

    (policy,) = view.policies
    assert policy.name == "github"
    github, public = policy.rules
    assert github.hosts == ["api.github.com"]
    assert github.methods == ["GET", "POST"]
    assert github.credential == CredentialView(header="Authorization", placeholder=PLACEHOLDER)
    assert public.credential is None, "a rule that substitutes nothing offers nothing to present"


def test_the_secret_and_its_whereabouts_are_absent_from_the_whole_document() -> None:
    """The value, the Secret it lives in and the key within it: a sandbox learns none of them, and
    this is checked over the serialised document so a field added anywhere fails it."""
    document = agent_view(_index(), SANDBOX, NOW).model_dump_json()

    assert PLACEHOLDER in document, "anchor: the projection is populated, so the absences below mean something"
    for forbidden in (SECRET_VALUE, "vault-entry", "credential-key", "secretRef", "secret_ref"):
        assert forbidden not in document, f"{forbidden!r} reached a sandbox-readable view"


def test_an_expired_binding_grants_nothing_and_says_nothing() -> None:
    """The view reads the same bindings the decision does, so it cannot advertise what is refused."""
    view = agent_view(_index(expires_at=NOW - timedelta(seconds=1)), SANDBOX, NOW)

    assert view.policies == []


def test_a_policy_that_does_not_exist_contributes_nothing_and_voids_nothing() -> None:
    """A binding grants whatever resolves: the missing name is absent, the rest still stands."""
    view = agent_view(_index(policies=["github", "gone"]), SANDBOX, NOW)

    assert [policy.name for policy in view.policies] == ["github"]


def test_a_binding_whose_every_policy_is_missing_grants_nothing() -> None:
    view = agent_view(_index(policies=["gone"]), SANDBOX, NOW)

    assert view.policies == []


def test_a_sandbox_no_binding_names_sees_an_empty_view_rather_than_an_error() -> None:
    """No egress is a normal state, not a failure: the answer is an empty list."""
    other = Sandbox(metadata=ObjectMeta(name="other", uid="other-uid"))

    view = agent_view(_index(), other, NOW)

    assert view.sandbox == "other"
    assert view.policies == []


if __name__ == "__main__":
    pytest_bazel.main()
