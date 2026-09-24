"""haku-ci's KEDA ScaledJob is watched by the KEDA release and authenticates with the Forgejo token it scales on."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
import yaml
from more_itertools import one


def test_haku_ci_keda_resources_are_wired_to_the_runner_job(k8s_dir: Path, generated_dir: Path) -> None:
    keda_release = one(
        doc
        for doc in yaml.safe_load_all((generated_dir / "keda/keda.k8s.yaml").read_text(encoding="utf-8"))
        if doc["kind"] == "HelmRelease"
    )
    haku_ci = list(yaml.safe_load_all((k8s_dir / "haku-ci/haku-ci.k8s.yaml").read_text(encoding="utf-8")))
    auth = one(obj for obj in haku_ci if obj["kind"] == "TriggerAuthentication")
    scaled_job = one(obj for obj in haku_ci if obj["kind"] == "ScaledJob")
    assert keda_release["spec"]["values"]["watchNamespace"] == scaled_job["metadata"]["namespace"]

    token_manifest = yaml.safe_load((k8s_dir / "haku/forgejo-tea/haku-forgejo-tea.sops.yaml").read_text())
    [secret_ref] = auth["spec"]["secretTargetRef"]
    assert secret_ref["name"] == token_manifest["metadata"]["name"]
    assert secret_ref["key"] in token_manifest["stringData"]

    annotations = token_manifest["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == auth["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == auth["metadata"]["namespace"]


if __name__ == "__main__":
    pytest_bazel.main()
