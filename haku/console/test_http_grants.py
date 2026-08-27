"""Domain contracts for temporary HTTP egress grants."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.grant_principal import AgentGrantPrincipal
from haku.console.http_grant_models import (
    HttpGrant,
    HttpGrantSpec,
    HttpGrantStatus,
    HttpMethod,
    HttpOrigin,
    HttpScheme,
    derive_status,
)

_CREATED = datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC)
_EXPIRES = datetime.datetime(2026, 8, 21, 1, tzinfo=datetime.UTC)


def origin(**overrides: object) -> HttpOrigin:
    payload: dict[str, object] = {"scheme": HttpScheme.HTTPS, "host": "example.com", "port": 443, **overrides}
    return HttpOrigin.model_validate(payload)


def spec(**overrides: object) -> HttpGrantSpec:
    payload: dict[str, object] = {"origin": origin(), "methods": [HttpMethod.GET], **overrides}
    return HttpGrantSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("host", "canonical"),
    [
        ("example.com", "example.com"),
        ("Example.COM", "example.com"),
        ("  example.com  ", "example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("0x7f.example", "0x7f.example"),
    ],
)
def test_origin_canonicalizes_host_to_lowercase_a_label(host: str, canonical: str) -> None:
    assert origin(host=host).host == canonical


def test_canonically_distinct_spellings_of_one_origin_compare_equal() -> None:
    assert origin(host="EXAMPLE.com") == origin(host="example.com")
    assert origin(host="bücher.example") == origin(host="xn--bcher-kva.example")


def test_origin_is_exact_no_component_generalizes() -> None:
    exact = origin()
    assert exact != origin(port=8443)
    assert exact != origin(scheme=HttpScheme.HTTP, port=443)
    assert exact != origin(host="other.example")
    assert exact != origin(host="www.example.com")


@pytest.mark.parametrize(
    ("host", "message"),
    [
        ("", "host must not be empty"),
        ("   ", "host must not be empty"),
        ("*", "wildcard"),
        ("*.example.com", "wildcard"),
        ("example.com.", "trailing dot"),
        ("example.com/path", "bare hostname"),
        ("user@example.com", "bare hostname"),
        ("example.com:443", "bare hostname"),
        ("[2001:db8::1]", "bare hostname"),
        ("2001:db8::1", "bare hostname"),
        ("192.168.0.1", "IP-literal"),
        ("127.1", "IP-literal"),
        ("2130706433", "IP-literal"),
        ("0x7f000001", "IP-literal"),
        ("192.168.1", "IP-literal"),
        ("exa mple.com", "not a valid IDNA hostname"),
        ("a_b.example", "not a valid IDNA hostname"),
        ("-bad.example", "not a valid IDNA hostname"),
        ("a..b", "not a valid IDNA hostname"),
        ("a" * 64 + ".example", "not a valid IDNA hostname"),
    ],
)
def test_origin_rejects_non_canonical_wildcard_url_and_ip_literal_hosts(host: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        origin(host=host)


@pytest.mark.parametrize("port", [0, -1, 65_536])
def test_origin_rejects_out_of_range_ports(port: int) -> None:
    with pytest.raises(ValidationError):
        origin(port=port)


def test_origin_requires_a_known_scheme_and_explicit_port() -> None:
    with pytest.raises(ValidationError):
        HttpOrigin.model_validate({"scheme": "ftp", "host": "example.com", "port": 21})
    with pytest.raises(ValidationError, match="port"):
        HttpOrigin.model_validate({"scheme": "https", "host": "example.com"})


def test_origin_serializes_canonically() -> None:
    assert origin(host="BÜCHER.example").model_dump(mode="json") == {
        "scheme": "https",
        "host": "xn--bcher-kva.example",
        "port": 443,
    }


def test_spec_requires_at_least_one_method_and_sorts_them_canonically() -> None:
    with pytest.raises(ValidationError):
        spec(methods=[])
    dumped = spec(methods=[HttpMethod.POST, HttpMethod.GET, HttpMethod.DELETE]).model_dump(mode="json")
    assert dumped["methods"] == ["DELETE", "GET", "POST"]


def test_spec_rejects_uncompilable_or_blank_path_regex() -> None:
    with pytest.raises(ValidationError, match="not a valid regular expression"):
        spec(path_regex="(unclosed")
    with pytest.raises(ValidationError):
        spec(path_regex="")


def test_spec_coverage_requires_method_and_full_path_match() -> None:
    coverage = spec(methods=[HttpMethod.GET, HttpMethod.HEAD], path_regex="/repos/agentydragon/.*")
    assert coverage.covers(method=HttpMethod.GET, path="/repos/agentydragon/ducktape")
    assert coverage.covers(method=HttpMethod.HEAD, path="/repos/agentydragon/")
    assert not coverage.covers(method=HttpMethod.POST, path="/repos/agentydragon/ducktape")
    # fullmatch, not search: a prefix or infix hit is not coverage.
    assert not coverage.covers(method=HttpMethod.GET, path="/evil/repos/agentydragon/x")
    assert not coverage.covers(method=HttpMethod.GET, path="/repos")
    unpinned = spec(methods=[HttpMethod.GET])
    assert unpinned.covers(method=HttpMethod.GET, path="/anything")


def test_grantable_methods_exclude_transport_and_diagnostic_verbs() -> None:
    assert {method.value for method in HttpMethod} & {"CONNECT", "TRACE"} == set()


def test_status_is_derived_from_end_facts_and_the_clock() -> None:
    early = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.UTC)
    assert derive_status(released_at=None, revoked_at=None, expires_at=_EXPIRES, now=_CREATED) is HttpGrantStatus.ACTIVE
    assert (
        derive_status(released_at=None, revoked_at=None, expires_at=_EXPIRES, now=_EXPIRES) is HttpGrantStatus.EXPIRED
    )
    assert derive_status(released_at=early, revoked_at=None, expires_at=_EXPIRES, now=_EXPIRES) is (
        HttpGrantStatus.RELEASED
    )
    assert derive_status(released_at=None, revoked_at=early, expires_at=_EXPIRES, now=_EXPIRES) is (
        HttpGrantStatus.REVOKED
    )
    # Expiration wins over an end action recorded at or past the time bound.
    assert derive_status(released_at=_EXPIRES, revoked_at=None, expires_at=_EXPIRES, now=_EXPIRES) is (
        HttpGrantStatus.EXPIRED
    )


def _grant_payload() -> dict[str, object]:
    return {
        "grant_id": UUID("00000000-0000-4000-8000-000000000001"),
        "owner_agent_id": UUID("00000000-0000-4000-8000-000000000002"),
        "principal": AgentGrantPrincipal(agent_id=UUID("00000000-0000-4000-8000-000000000002")),
        "source_tool_call_id": "tc_source",
        "spec": spec(),
        "status": HttpGrantStatus.ACTIVE,
        "created_at": _CREATED,
        "expires_at": _EXPIRES,
    }


@pytest.mark.parametrize("field", ["created_at", "expires_at", "released_at"])
def test_grant_timestamps_require_timezone_awareness(field: str) -> None:
    payload = _grant_payload()
    if field == "released_at":
        payload.update(status=HttpGrantStatus.RELEASED, end_reason="done")
    payload[field] = datetime.datetime(2026, 8, 21)

    with pytest.raises(ValidationError):
        HttpGrant.model_validate(payload)


def test_agent_grant_principal_must_belong_to_lifecycle_owner() -> None:
    payload = _grant_payload()
    payload["principal"] = AgentGrantPrincipal(agent_id=UUID(int=3))

    with pytest.raises(ValidationError, match="must belong to the lifecycle owner"):
        HttpGrant.model_validate(payload)


def test_grant_end_facts_travel_together_and_match_the_status() -> None:
    early = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.UTC)

    payload = _grant_payload()
    payload["status"] = HttpGrantStatus.REVOKED
    with pytest.raises(ValidationError, match="revoked grant requires revoked_at"):
        HttpGrant.model_validate(payload)

    payload = _grant_payload()
    payload.update(released_at=early, end_reason="early")
    with pytest.raises(ValidationError, match="an active grant cannot carry end facts"):
        HttpGrant.model_validate(payload)

    payload = _grant_payload()
    payload.update(status=HttpGrantStatus.RELEASED, released_at=early)
    with pytest.raises(ValidationError, match="end_reason travels exactly with a recorded end action"):
        HttpGrant.model_validate(payload)

    payload = _grant_payload()
    payload.update(status=HttpGrantStatus.RELEASED, released_at=early, revoked_at=early, end_reason="both")
    with pytest.raises(ValidationError, match="cannot be both released and revoked"):
        HttpGrant.model_validate(payload)


if __name__ == "__main__":
    pytest_bazel.main()
