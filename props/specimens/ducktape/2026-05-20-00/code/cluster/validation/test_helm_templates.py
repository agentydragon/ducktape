"""Bazel test for Helm template validation."""

import asyncio

import pytest_bazel

from cluster.validation.helm_templates import validate_helm_templates


def test_helm_templates_render() -> None:
    errors = asyncio.run(validate_helm_templates())
    assert not errors, "\n".join(errors)


if __name__ == "__main__":
    pytest_bazel.main()
