"""haku-ci's KEDA ScaledJob is wired to the KEDA release and the Forgejo token it scales on."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
import yaml


def test_haku_ci_keda_resources_are_wired_to_the_runner_job(k8s_dir: Path) -> None:
    keda = {
        doc["kind"]: doc for doc in yaml.safe_load_all((k8s_dir / "keda/keda.k8s.yaml").read_text(encoding="utf-8"))
    }
    keda_repository, keda_release = keda["HelmRepository"], keda["HelmRelease"]
    auth, scaled_job = list(yaml.safe_load_all((k8s_dir / "haku-ci/scaledjob.yaml").read_text(encoding="utf-8")))
    source_ref = keda_release["spec"]["chart"]["spec"]["sourceRef"]
    assert (source_ref["name"], source_ref["namespace"]) == (
        keda_repository["metadata"]["name"],
        keda_repository["metadata"]["namespace"],
    )
    assert keda_release["spec"]["values"]["watchNamespace"] == scaled_job["metadata"]["namespace"]

    token_manifest = yaml.safe_load((k8s_dir / "haku/forgejo-tea/haku-forgejo-tea.sops.yaml").read_text())
    [secret_ref] = auth["spec"]["secretTargetRef"]
    assert secret_ref["name"] == token_manifest["metadata"]["name"]
    assert secret_ref["key"] in token_manifest["stringData"]

    [trigger] = scaled_job["spec"]["triggers"]
    assert trigger["authenticationRef"]["name"] == auth["metadata"]["name"]

    annotations = token_manifest["metadata"]["annotations"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces"] == auth["metadata"]["namespace"]
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-namespaces"] == auth["metadata"]["namespace"]


if __name__ == "__main__":
    pytest_bazel.main()
