"""haku-ci's KEDA TriggerAuthentication reads the hand-written Forgejo token Secret it scales on."""

from __future__ import annotations

import pytest_bazel
import yaml
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s.haku_ci import runner
from util.bazel.runfiles import get_required_path


def test_keda_auth_reads_the_forgejo_token_secret() -> None:
    auth = one(
        obj for obj in Cdk8sTesting.synth(runner.chart(Cdk8sTesting.app())) if obj["kind"] == "TriggerAuthentication"
    )
    token = yaml.safe_load(
        get_required_path("_main/cluster/k8s/haku/forgejo-tea/haku-forgejo-tea.sops.yaml").read_text()
    )

    [secret_ref] = auth["spec"]["secretTargetRef"]
    assert secret_ref["name"] == token["metadata"]["name"]
    assert secret_ref["key"] in token["stringData"]
    # Reflector copies the Secret from haku-sandbox into the namespace KEDA reads it in.
    annotations = token["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == auth["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == auth["metadata"]["namespace"]


if __name__ == "__main__":
    pytest_bazel.main()
