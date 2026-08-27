"""Wire contracts of the decide call: secret masking, resolution pinning, verdict parsing."""

from __future__ import annotations

import datetime
import json
from ipaddress import IPv4Address, IPv6Address

import pytest
import pytest_bazel
from pydantic import SecretStr, TypeAdapter, ValidationError

from haku.egress.decision import DecideAllowed, DecideDenied, DecideRequest, DecideResponse, DecisionSource, RequestMeta

_FENCE = "agent-fence-credential"


def _request(**overrides: object) -> DecideRequest:
    fields: dict[str, object] = {
        "fence_credential": SecretStr(_FENCE),
        "request": RequestMeta(method="GET", scheme="https", host="api.example", port=443, path="/api/items?x=1"),
        "resolved_ips": frozenset({IPv4Address("192.0.2.10"), IPv6Address("2001:db8::10")}),
        "upstream_ip": IPv4Address("192.0.2.10"),
        **overrides,
    }
    return DecideRequest.model_validate(fields)


def test_fence_credential_travels_on_the_wire_but_masks_everywhere_else() -> None:
    request = _request()

    wire = request.model_dump_json()

    assert json.loads(wire)["fence_credential"] == _FENCE
    assert _FENCE not in repr(request)
    assert _FENCE not in str(request)
    # The parsed form on the Console side masks identically.
    assert _FENCE not in repr(DecideRequest.model_validate_json(wire))


def test_wire_round_trip_preserves_the_request() -> None:
    request = _request()

    parsed = DecideRequest.model_validate_json(request.model_dump_json())

    assert parsed.request == request.request
    assert parsed.resolved_ips == request.resolved_ips
    assert parsed.upstream_ip == request.upstream_ip
    assert parsed.fence_credential.get_secret_value() == _FENCE


def test_resolved_ips_serialize_deterministically() -> None:
    ips = frozenset({IPv4Address("192.0.2.10"), IPv4Address("192.0.2.2"), IPv6Address("2001:db8::10")})

    wire = json.loads(_request(resolved_ips=ips, upstream_ip=IPv4Address("192.0.2.2")).model_dump_json())

    assert wire["resolved_ips"] == ["192.0.2.2", "192.0.2.10", "2001:db8::10"]


def test_upstream_ip_must_be_one_of_the_resolved_addresses() -> None:
    with pytest.raises(ValidationError, match="upstream_ip must be one of resolved_ips"):
        _request(upstream_ip=IPv4Address("198.51.100.9"))


def test_resolution_must_be_present_and_bounded() -> None:
    with pytest.raises(ValidationError):
        _request(resolved_ips=frozenset())
    oversized = frozenset(IPv4Address(f"203.0.{block}.{host}") for block in range(2) for host in range(250))
    with pytest.raises(ValidationError):
        _request(resolved_ips=oversized, upstream_ip=IPv4Address("203.0.0.0"))


def test_verdicts_parse_by_their_allowed_discriminant() -> None:
    adapter: TypeAdapter[DecideResponse] = TypeAdapter(DecideResponse)

    allowed = adapter.validate_json(
        json.dumps(
            {
                "allowed": True,
                "source": "grant",
                "decision_id": "grant:50000000-0000-4000-8000-000000000005",
                "valid_until": "2026-08-27T12:30:00Z",
                "substitutions": [
                    {
                        "placeholder": "github-token-placeholder",
                        "value": "real-value",
                        "match_headers": ["authorization"],
                    }
                ],
            }
        )
    )
    assert isinstance(allowed, DecideAllowed)
    assert allowed.source is DecisionSource.GRANT
    assert allowed.valid_until == datetime.datetime(2026, 8, 27, 12, 30, tzinfo=datetime.UTC)
    (substitution,) = allowed.substitutions
    assert substitution.match_headers == frozenset({"authorization"})

    # A standing admission carries no deadline: the Console route omits the None field
    # (response_model_exclude_none), and the parse restores it.
    standing = adapter.validate_json(
        json.dumps({"allowed": True, "source": "standing", "decision_id": "standing:haku-github-api"})
    )
    assert isinstance(standing, DecideAllowed)
    assert standing.source is DecisionSource.STANDING
    assert standing.valid_until is None
    assert standing.substitutions == []

    denied = adapter.validate_json(json.dumps({"allowed": False, "source": "none", "reason": "no grant"}))
    assert isinstance(denied, DecideDenied)
    assert denied.grant_scope is None


def test_denials_never_claim_an_admitting_source() -> None:
    with pytest.raises(ValidationError):
        DecideDenied.model_validate({"allowed": False, "source": "grant", "reason": "contradiction"})
    with pytest.raises(ValidationError):
        DecideAllowed.model_validate(
            {"allowed": True, "source": "none", "decision_id": "x", "valid_until": "2026-08-27T12:30:00Z"}
        )


if __name__ == "__main__":
    pytest_bazel.main()
