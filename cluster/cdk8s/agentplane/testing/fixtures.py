"""Read staging charts synthesized from current sources by a shared Bazel action."""

import json
from typing import Any, cast

import pytest
from python.runfiles import Runfiles


@pytest.fixture(scope="session")
def rendered_catalogs(runfiles: Runfiles) -> dict[str, list[dict[str, Any]]]:
    return cast(
        dict[str, list[dict[str, Any]]],
        json.loads((runfiles.root() / "ducktape/cluster/cdk8s/agentplane/testing/catalogs.json").read_text()),
    )


@pytest.fixture(scope="session")
def staging_manifests(rendered_catalogs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return rendered_catalogs["staging"]


@pytest.fixture(scope="session")
def identity_catalog_manifests(rendered_catalogs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Staging with its policy catalog in the identity shapes, which staging does not render yet."""
    return rendered_catalogs["identity_catalog"]


@pytest.fixture(scope="session", params=[False, True], ids=["serviceaccount-catalog", "identity-catalog"])
def catalog_manifests(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    """Staging under each policy catalog rendering, for invariants both must hold."""
    return cast(
        list[dict[str, Any]],
        request.getfixturevalue("identity_catalog_manifests" if request.param else "staging_manifests"),
    )
