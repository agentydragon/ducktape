"""Domain contracts for temporary HTTP egress grants."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.grants.envelope import GrantStatus, derive_status
from haku.console.grants.http.models import Grant, GrantSpec, HttpMethod, HttpOrigin, HttpRequestCoverage, HttpScheme
from haku.console.grants.principal import AgentGrantPrincipal

_CREATED = datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC)
_EXPIRES = datetime.datetime(2026, 8, 21, 1, tzinfo=datetime.UTC)


def origin(**overrides: object) -> HttpOrigin:
    payload: dict[str, object] = {"scheme": HttpScheme.HTTPS, "host": "example.com", "port": 443, **overrides}
    return HttpOrigin.model_validate(payload)


def coverage(**overrides: object) -> HttpRequestCoverage:
    payload: dict[str, object] = {"methods": [HttpMethod.GET], **overrides}
    return HttpRequestCoverage.model_validate(payload)


def spec(**overrides: object) -> GrantSpec:
    """Build a grant spec; ``methods``/``path_regex`` overrides populate the nested coverage."""
    coverage_fields = {key: overrides.pop(key) for key in ("methods", "path_regex") if key in overrides}
    payload: dict[str, object] = {"origin": origin(), "coverage": coverage(**coverage_fields), **overrides}
    return GrantSpec.model_validate(payload)


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


def test_coverage_requires_at_least_one_method_and_sorts_them_canonically() -> None:
    with pytest.raises(ValidationError):
        coverage(methods=[])
    dumped = coverage(methods=[HttpMethod.POST, HttpMethod.GET, HttpMethod.DELETE]).model_dump(mode="json")
    assert dumped["methods"] == ["DELETE", "GET", "POST"]


def test_coverage_rejects_uncompilable_or_blank_path_regex() -> None:
    with pytest.raises(ValidationError, match="not a valid regular expression"):
        coverage(path_regex="(unclosed")
    with pytest.raises(ValidationError):
        coverage(path_regex="")


def test_coverage_requires_method_and_full_path_match() -> None:
    pinned = coverage(methods=[HttpMethod.GET, HttpMethod.HEAD], path_regex="/repos/agentydragon/.*")
    assert pinned.covers(method=HttpMethod.GET, path="/repos/agentydragon/ducktape")
    assert pinned.covers(method=HttpMethod.HEAD, path="/repos/agentydragon/")
    assert not pinned.covers(method=HttpMethod.POST, path="/repos/agentydragon/ducktape")
    # fullmatch, not search: a prefix or infix hit is not coverage.
    assert not pinned.covers(method=HttpMethod.GET, path="/evil/repos/agentydragon/x")
    assert not pinned.covers(method=HttpMethod.GET, path="/repos")
    unpinned = coverage(methods=[HttpMethod.GET])
    assert unpinned.covers(method=HttpMethod.GET, path="/anything")


def test_grantable_methods_exclude_transport_and_diagnostic_verbs() -> None:
    assert {method.value for method in HttpMethod} & {"CONNECT", "TRACE"} == set()


def test_grant_spec_allow_prohibited_address_defaults_off_and_round_trips() -> None:
    assert spec().allow_prohibited_address is False
    flagged = spec(allow_prohibited_address=True)
    assert flagged.allow_prohibited_address is True
    assert flagged.model_dump(mode="json")["allow_prohibited_address"] is True
    assert GrantSpec.model_validate(flagged.model_dump(mode="json")) == flagged


def test_status_is_derived_from_end_facts_and_the_clock() -> None:
    early = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.UTC)
    assert derive_status(released_at=None, revoked_at=None, expires_at=_EXPIRES, now=_CREATED) is GrantStatus.ACTIVE
    assert derive_status(released_at=None, revoked_at=None, expires_at=_EXPIRES, now=_EXPIRES) is GrantStatus.EXPIRED
    assert derive_status(released_at=early, revoked_at=None, expires_at=_EXPIRES, now=_EXPIRES) is (
        GrantStatus.RELEASED
    )
    assert derive_status(released_at=None, revoked_at=early, expires_at=_EXPIRES, now=_EXPIRES) is (GrantStatus.REVOKED)
    # Expiration wins over an end action recorded at or past the time bound.
    assert derive_status(released_at=_EXPIRES, revoked_at=None, expires_at=_EXPIRES, now=_EXPIRES) is (
        GrantStatus.EXPIRED
    )


def _grant_payload() -> dict[str, object]:
    return {
        "grant_id": UUID("00000000-0000-4000-8000-000000000001"),
        "owner_agent_id": UUID("00000000-0000-4000-8000-000000000002"),
        "principal": AgentGrantPrincipal(agent_id=UUID("00000000-0000-4000-8000-000000000002")),
        "source_tool_call_id": "tc_source",
        "spec": spec(),
        "created_at": _CREATED,
        "expires_at": _EXPIRES,
    }


@pytest.mark.parametrize("field", ["created_at", "expires_at", "released_at"])
def test_grant_timestamps_require_timezone_awareness(field: str) -> None:
    payload = _grant_payload()
    if field == "released_at":
        payload.update(end_reason="done")
    payload[field] = datetime.datetime(2026, 8, 21)

    with pytest.raises(ValidationError):
        Grant.model_validate(payload)


def test_agent_grant_principal_must_belong_to_lifecycle_owner() -> None:
    payload = _grant_payload()
    payload["principal"] = AgentGrantPrincipal(agent_id=UUID(int=3))

    with pytest.raises(ValidationError, match="must belong to the lifecycle owner"):
        Grant.model_validate(payload)


def test_grant_end_facts_travel_together() -> None:
    early = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.UTC)

    payload = _grant_payload()
    payload.update(released_at=early)
    with pytest.raises(ValidationError, match="end_reason travels exactly with a recorded end action"):
        Grant.model_validate(payload)

    payload = _grant_payload()
    payload.update(released_at=early, revoked_at=early, end_reason="both")
    with pytest.raises(ValidationError, match="cannot be both released and revoked"):
        Grant.model_validate(payload)


def test_status_is_computed_from_facts_and_clock() -> None:
    early = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.UTC)

    # _EXPIRES is in the past, so with no end fact the computed status is EXPIRED.
    expired = Grant.model_validate(_grant_payload())
    assert expired.status is GrantStatus.EXPIRED

    released = Grant.model_validate({**_grant_payload(), "released_at": early, "end_reason": "done"})
    assert released.status is GrantStatus.RELEASED
    # The computed field still serializes: the wire keeps its status key.
    assert released.model_dump()["status"] is GrantStatus.RELEASED

    active_payload = _grant_payload()
    active_payload["expires_at"] = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    assert Grant.model_validate(active_payload).status is GrantStatus.ACTIVE


if __name__ == "__main__":
    pytest_bazel.main()
