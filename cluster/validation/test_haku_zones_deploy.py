"""Render the parked Haku worker-zone deployment package."""

from __future__ import annotations

import asyncio

import pytest_bazel

from cluster.validation.kustomize import run_kustomize_build
from util.bazel.runfiles import get_required_path


def test_haku_zones_deploy_renders() -> None:
    result = asyncio.run(run_kustomize_build(get_required_path("ducktape/haku/x/zones/deploy/kustomization.yaml")))
    resources = {(resource.kind, resource.name, resource.namespace) for resource in result.resources}

    assert ("Namespace", "haku-zones-mitmproxy", "") in resources
    assert ("Deployment", "haku-zones-mitmproxy", "haku-zones-mitmproxy") in resources
    assert ("Namespace", "haku-sandbox-zai", "") in resources
    assert ("ServiceAccount", "haku-zone-worker", "haku-sandbox-zai") in resources
    assert ("ClusterPolicy", "inject-haku-zones-mitmproxy", "") in resources


if __name__ == "__main__":
    pytest_bazel.main()
