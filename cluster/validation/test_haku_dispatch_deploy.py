"""Render the parked Haku dispatch deployment package."""

from __future__ import annotations

import asyncio

import pytest_bazel

from cluster.validation.kustomize import run_kustomize_build
from util.bazel.runfiles import get_required_path


def test_haku_dispatch_deploy_renders() -> None:
    result = asyncio.run(run_kustomize_build(get_required_path("ducktape/haku/x/dispatch/deploy/kustomization.yaml")))
    resources = {(resource.kind, resource.name, resource.namespace) for resource in result.resources}

    assert ("Namespace", "haku-dispatch", "") in resources
    assert ("Deployment", "workers-litellm", "haku-dispatch") in resources
    assert ("Deployment", "dispatcher", "haku-dispatch") in resources


if __name__ == "__main__":
    pytest_bazel.main()
