"""Bazel test for Helm template validation."""

import asyncio

from cluster.validation.helm_templates import validate_helm_templates


def test_helm_templates_render() -> None:
    errors = asyncio.run(validate_helm_templates())
    assert not errors, "\n".join(errors)
