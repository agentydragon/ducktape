"""The projection a sandbox may read of its own egress: what it says, and what it can never say."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from agentplane.egress.agent_view import CredentialView, agent_view
from agentplane.egress.policy import Index
from agentplane.egress.resources import (
    BasicPasswordTarget,
    BindingSpec,
    CredentialRef,
    CredentialSource,
    CredentialSpec,
    EgressBinding,
    EgressCredential,
    EgressPolicy,
    JsonFieldTarget,
    ObjectMeta,
    PolicySpec,
    Rule,
    SchemeTokenTarget,
    Secret,
    SecretKeyRef,
    Target,
    TargetMethod,
)
from agentplane.subjects import ServiceAccountRef

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
SECRET_VALUE = "the-real-credential"
DESCRIPTION = "a token for the bot account, which can write to its own repositories"
NAMESPACE = "agentplane-test"
CALLER = ServiceAccountRef(namespace=NAMESPACE, name="sb")
CREDENTIAL = EgressCredential(
    metadata=ObjectMeta(name="github-pat", generation=1),
    spec=CredentialSpec(
        source=CredentialSource(secret_ref=SecretKeyRef(name="vault-entry", key="credential-key")),
        description=DESCRIPTION,
        targets=[
            SchemeTokenTarget(header="Authorization", method=TargetMethod.SCHEME_TOKEN, scheme="Bearer"),
            BasicPasswordTarget(header="Authorization", method=TargetMethod.BASIC_PASSWORD),
        ],
    ),
)
PLACEHOLDER = CREDENTIAL.placeholder
GITHUB_RULE = Rule(
    hosts=["api.github.com"],
    methods=["GET", "POST"],
    paths=["/repos/**"],
    credential_ref=CredentialRef(name=CREDENTIAL.metadata.name),
)
OPEN_RULE = Rule(hosts=["*.example.com"])


def _index(*, expires_at: datetime | None = None, policies: list[str] | None = None) -> Index:
    policy = EgressPolicy(
        metadata=ObjectMeta(name="github", generation=1), spec=PolicySpec(rules=[GITHUB_RULE, OPEN_RULE])
    )
    bound = EgressBinding(
        metadata=ObjectMeta(name="b", generation=1),
        spec=BindingSpec(
            subjects=[CALLER], policies=policies if policies is not None else ["github"], expires_at=expires_at
        ),
    )
    return Index(
        policies={"github": policy},
        bindings={"b": bound},
        credentials={CREDENTIAL.metadata.name: CREDENTIAL},
        secrets={"vault-entry": Secret(name="vault-entry", data={"credential-key": SECRET_VALUE})},
    )


def test_a_sandbox_is_told_every_target_and_not_just_the_placeholder() -> None:
    """Enough to build a request the proxy substitutes into. A placeholder and a header name are not:
    a client still has to know whether the value reads `Bearer <placeholder>` or the placeholder
    bare, and getting that wrong is a 401 from the upstream with the real credential in the header.

    The description rides along: targets say how to present the credential, the description says
    what presenting it does, and an agent given only an opaque placeholder cannot weigh whether it
    should.
    """
    view = agent_view(_index(), CALLER, NOW)

    (policy,) = view.policies
    assert policy.name == "github"
    github, public = policy.rules
    assert github.hosts == ["api.github.com"]
    assert github.methods == ["GET", "POST"]
    assert github.credential == CredentialView(
        name="github-pat",
        description=DESCRIPTION,
        placeholder=PLACEHOLDER,
        targets=[
            SchemeTokenTarget(header="Authorization", method=TargetMethod.SCHEME_TOKEN, scheme="Bearer"),
            BasicPasswordTarget(header="Authorization", method=TargetMethod.BASIC_PASSWORD),
        ],
    )
    assert public.credential is None, "a rule that substitutes nothing offers nothing to present"


def test_the_secret_and_its_whereabouts_are_absent_from_the_whole_document() -> None:
    """The value, the Secret it lives in and the key within it: a sandbox learns none of them, and
    this is checked over the serialised document so a field added anywhere fails it."""
    document = agent_view(_index(), CALLER, NOW).model_dump_json()

    assert PLACEHOLDER in document, "anchor: the projection is populated, so the absences below mean something"
    for forbidden in (SECRET_VALUE, "vault-entry", "credential-key", "secretRef", "secret_ref"):
        assert forbidden not in document, f"{forbidden!r} reached a sandbox-readable view"


def test_an_expired_binding_grants_nothing_and_says_nothing() -> None:
    """The view reads the same bindings the decision does, so it cannot advertise what is refused."""
    view = agent_view(_index(expires_at=NOW - timedelta(seconds=1)), CALLER, NOW)

    assert view.policies == []


def test_a_policy_that_does_not_exist_contributes_nothing_and_voids_nothing() -> None:
    """A binding grants whatever resolves: the missing name is absent, the rest still stands."""
    view = agent_view(_index(policies=["github", "gone"]), CALLER, NOW)

    assert [policy.name for policy in view.policies] == ["github"]


def test_a_binding_whose_every_policy_is_missing_grants_nothing() -> None:
    view = agent_view(_index(policies=["gone"]), CALLER, NOW)

    assert view.policies == []


def test_a_subject_no_binding_names_sees_an_empty_view_rather_than_an_error() -> None:
    """No egress is a normal state, not a failure: the answer is an empty list."""
    other = ServiceAccountRef(namespace=NAMESPACE, name="other")

    view = agent_view(_index(), other, NOW)

    assert view.subject == other
    assert view.policies == []


def test_json_target_is_discoverable_without_exposing_credential_source_or_value() -> None:
    rules = _index()
    rules.credentials[CREDENTIAL.metadata.name] = CREDENTIAL.model_copy(
        update={
            "spec": CREDENTIAL.spec.model_copy(
                update={"targets": [JsonFieldTarget(method=TargetMethod.JSON_FIELD, field="password")]}
            )
        }
    )
    view = agent_view(rules, CALLER, NOW)
    target = view.policies[0].rules[0].credential
    assert target is not None
    assert target.targets == [JsonFieldTarget(method=TargetMethod.JSON_FIELD, field="password")]
    assert SECRET_VALUE not in view.model_dump_json()
    assert "vault-entry" not in view.model_dump_json()


@pytest.mark.parametrize(
    "target",
    [
        {"method": "wholeValue", "header": "X-Key"},
        {"method": "schemeToken", "header": "Authorization", "scheme": "Bearer"},
        {"method": "basicUsername", "header": "Authorization"},
        {"method": "basicPassword", "header": "Authorization"},
        {"method": "basicWhole", "header": "Authorization"},
        {"method": "jsonField", "field": "password"},
    ],
)
def test_agent_target_variants_serialize_only_their_own_fields(target: dict[str, str]) -> None:
    view = CredentialView.model_validate(
        {"name": "test", "description": "test credential", "placeholder": "test-placeholder", "targets": [target]}
    )
    assert view.model_dump(mode="json")["targets"] == [target]
    assert TypeAdapter(Target).validate_python(target).model_dump(mode="json") == target


@pytest.mark.parametrize(
    "target",
    [
        {"method": "jsonField", "field": "password", "header": "Authorization"},
        {"method": "jsonField", "field": "password", "scheme": "Bearer"},
        {"method": "jsonField", "field": "password", "header": None},
        {"method": "jsonField", "header": "Authorization"},
        {"method": "wholeValue", "header": "X-Key", "field": "password"},
        {"method": "wholeValue", "header": "X-Key", "scheme": "Bearer"},
        {"method": "schemeToken", "header": "Authorization"},
        {"method": "basicPassword", "header": "Authorization", "scheme": None},
        {"method": "unknown", "header": "Authorization"},
        {"header": "Authorization"},
    ],
)
def test_mixed_or_incomplete_targets_are_rejected_by_resources_and_agent_api(target: dict[str, str | None]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Target).validate_python(target)
    with pytest.raises(ValidationError):
        CredentialView.model_validate(
            {"name": "test", "description": "test credential", "placeholder": "test-placeholder", "targets": [target]}
        )


def test_agent_schema_exposes_a_discriminated_union_not_nullable_sibling_fields() -> None:
    schema = CredentialView.model_json_schema()
    items = schema["properties"]["targets"]["items"]
    assert items["discriminator"]["propertyName"] == "method"
    assert set(items["discriminator"]["mapping"]) == {method.value for method in TargetMethod}
    assert len(items["oneOf"]) == len(TargetMethod)
    assert set(schema["$defs"]["JsonFieldTarget"]["properties"]) == {"method", "field"}
    assert set(schema["$defs"]["SchemeTokenTarget"]["properties"]) == {"method", "header", "scheme"}
    assert set(schema["$defs"]["BasicPasswordTarget"]["properties"]) == {"method", "header"}


if __name__ == "__main__":
    pytest_bazel.main()
