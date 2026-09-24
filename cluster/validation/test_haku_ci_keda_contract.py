"""haku-ci's KEDA TriggerAuthentication reads the hand-written Forgejo token Secret it scales on."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
import yaml
from more_itertools import one


def test_haku_ci_keda_auth_reads_the_forgejo_token_secret(k8s_dir: Path) -> None:
    haku_ci = list(yaml.safe_load_all((k8s_dir / "haku-ci/haku-ci.k8s.yaml").read_text(encoding="utf-8")))
    auth = one(obj for obj in haku_ci if obj["kind"] == "TriggerAuthentication")

    token_manifest = yaml.safe_load((k8s_dir / "haku/forgejo-tea/haku-forgejo-tea.sops.yaml").read_text())
    [secret_ref] = auth["spec"]["secretTargetRef"]
    assert secret_ref["name"] == token_manifest["metadata"]["name"]
    assert secret_ref["key"] in token_manifest["stringData"]

    annotations = token_manifest["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == auth["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == auth["metadata"]["namespace"]


if __name__ == "__main__":
    pytest_bazel.main()
