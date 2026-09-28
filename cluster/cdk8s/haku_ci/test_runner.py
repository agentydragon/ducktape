"""haku-ci's KEDA TriggerAuthentication reads the hand-written Forgejo token Secret it scales on."""

from __future__ import annotations

import pytest_bazel
import yaml

from cluster.cdk8s.haku_ci import runner
from util.bazel.runfiles import get_required_path


def test_keda_auth_reads_the_forgejo_token_secret() -> None:
    token = yaml.safe_load(
        get_required_path("_main/cluster/k8s/haku/forgejo-tea/haku-forgejo-tea.sops.yaml").read_text()
    )

    assert token["metadata"]["name"] == runner.FORGEJO_TOKEN_SECRET
    assert runner.FORGEJO_TOKEN_KEY in token["stringData"]
    # Reflector copies the Secret from haku-sandbox into the namespace KEDA reads it in.
    annotations = token["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == runner.NAMESPACE
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == runner.NAMESPACE


if __name__ == "__main__":
    pytest_bazel.main()
