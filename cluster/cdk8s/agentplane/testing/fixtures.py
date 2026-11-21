"""Read staging charts synthesized from current sources by a shared Bazel action."""

import json
from typing import Any, cast

import pytest
from python.runfiles import Runfiles


def _read_catalog(runfiles: Runfiles, name: str) -> list[dict[str, Any]]:
    catalogs = json.loads((runfiles.root() / "ducktape/cluster/cdk8s/agentplane/testing/catalogs.json").read_text())
    return cast(list[dict[str, Any]], catalogs[name])


@pytest.fixture(scope="session")
def staging_manifests(runfiles: Runfiles) -> list[dict[str, Any]]:
    return _read_catalog(runfiles, "staging")


@pytest.fixture(scope="session")
def identity_catalog_manifests(runfiles: Runfiles) -> list[dict[str, Any]]:
    """Staging with its policy catalog in the identity shapes, which staging does not render yet."""
    return _read_catalog(runfiles, "identity_catalog")


@pytest.fixture(scope="session", params=[False, True], ids=["serviceaccount-catalog", "identity-catalog"])
def catalog_manifests(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    """Staging under each policy catalog rendering, for invariants both must hold."""
    return cast(
        list[dict[str, Any]],
        request.getfixturevalue("identity_catalog_manifests" if request.param else "staging_manifests"),
    )
