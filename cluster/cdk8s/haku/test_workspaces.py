"""The Haku sandbox pod carries no Kubernetes credential of its own."""

from __future__ import annotations

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.haku import workspaces


def test_haku_sandbox_mounts_no_service_account_token() -> None:
    """The box reaches Kubernetes only through the Console-mediated proxy
    (`HAKU_KUBERNETES_PROXY_URL`), never as a ServiceAccount."""
    template = one(
        obj
        for obj in Cdk8sTesting.synth(workspaces.chart(Cdk8sTesting.app()))
        if obj["kind"] == "SandboxTemplate" and obj["metadata"]["name"] == "haku"
    )
    pod = template["spec"]["podTemplate"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod


if __name__ == "__main__":
    pytest_bazel.main()
