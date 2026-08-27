"""Domain contracts for temporary exact-origin HTTP grants."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.grant_principal import AgentGrantPrincipal
from haku.console.http_grant_models import HttpGrant, HttpGrantStatus, HttpOrigin, HttpScheme


def origin(**overrides: object) -> HttpOrigin:
    payload: dict[str, object] = {"scheme": HttpScheme.HTTPS, "host": "example.com", "port": 443, **overrides}
    return HttpOrigin.model_validate(payload)


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


def _grant_payload() -> dict[str, object]:
    return {
        "grant_id": UUID("00000000-0000-4000-8000-000000000001"),
        "owner_agent_id": UUID("00000000-0000-4000-8000-000000000002"),
        "principal": AgentGrantPrincipal(agent_id=UUID("00000000-0000-4000-8000-000000000002")),
        "source_tool_call_id": "tc_source",
        "origin": origin(),
        "status": HttpGrantStatus.ACTIVE,
        "created_at": datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC),
        "expires_at": datetime.datetime(2026, 8, 21, 1, tzinfo=datetime.UTC),
    }


@pytest.mark.parametrize("field", ["created_at", "expires_at", "ended_at"])
def test_grant_timestamps_require_timezone_awareness(field: str) -> None:
    payload = _grant_payload()
    if field == "ended_at":
        payload.update(status=HttpGrantStatus.RELEASED, end_reason="done")
    payload[field] = datetime.datetime(2026, 8, 21)

    with pytest.raises(ValidationError, match=rf"{field} must be timezone-aware"):
        HttpGrant.model_validate(payload)


def test_agent_grant_principal_must_belong_to_lifecycle_owner() -> None:
    payload = _grant_payload()
    payload["principal"] = AgentGrantPrincipal(agent_id=UUID(int=3))

    with pytest.raises(ValidationError, match="must belong to the lifecycle owner"):
        HttpGrant.model_validate(payload)


def test_grant_terminal_state_requires_ended_at_and_reason() -> None:
    payload = _grant_payload()
    payload["status"] = HttpGrantStatus.REVOKED

    with pytest.raises(ValidationError, match="requires ended_at and a non-empty end_reason"):
        HttpGrant.model_validate(payload)

    payload = _grant_payload()
    payload.update(ended_at=datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.UTC), end_reason="early")
    with pytest.raises(ValidationError, match="an active grant cannot have terminal fields"):
        HttpGrant.model_validate(payload)


if __name__ == "__main__":
    pytest_bazel.main()
