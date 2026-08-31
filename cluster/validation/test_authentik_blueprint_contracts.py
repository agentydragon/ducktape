"""Focused contracts over the deployed Authentik blueprint set."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.authentik_blueprints import (
    check_blueprint_completeness,
    check_outpost_provider_references,
    check_proxy_provider_outpost_assignment,
)
from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def test_blueprint_completeness(k8s_dir: Path) -> None:
    """All Authentik blueprint YAML files must be listed in configMapGenerator."""
    errors = check_blueprint_completeness(k8s_dir)
    assert not errors, "\n".join(errors)


def test_proxy_providers_assigned_to_outpost(k8s_dir: Path) -> None:
    """Every present Authentik proxy provider must be assigned to an outpost.

    An unassigned proxy provider (HTTPRoute present, but not on the embedded outpost)
    302s to a login flow served on its own host, breaking Google SSO with
    redirect_uri_mismatch — the haku.allegedly.works failure mode.
    """
    errors = check_proxy_provider_outpost_assignment(k8s_dir)
    assert not errors, "\n".join(errors)


def test_outpost_provider_references_resolve(k8s_dir: Path) -> None:
    """Embedded-outpost refs are valid and do not target retired providers."""
    errors = check_outpost_provider_references(k8s_dir)
    assert not errors, "\n".join(errors)


if __name__ == "__main__":
    pytest_bazel.main()
