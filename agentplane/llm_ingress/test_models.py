import pytest_bazel

from agentplane.llm_ingress.models import (
    AttributionConfig,
    AttributionField,
    AttributionJsonHeader,
    VerifiedClaim,
    default_attribution,
)

_IDENTITY = AttributionField(key="identity", source=VerifiedClaim.IDENTITY)
_NAMESPACE = AttributionField(key="namespace", source=VerifiedClaim.NAMESPACE)


def test_default_attribution_names_identity():
    assert default_attribution().names_identity()


def test_every_output_must_name_identity():
    header = AttributionJsonHeader(name="x-verified", fields=[_IDENTITY])
    assert AttributionConfig(json_headers=[header], baggage=[_IDENTITY]).names_identity()
    assert not AttributionConfig(json_headers=[header], baggage=[_NAMESPACE]).names_identity()


if __name__ == "__main__":
    pytest_bazel.main()
