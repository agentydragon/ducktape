from pathlib import Path

import pytest_bazel

from cluster.validation.kustomize import flux_generated_kustomization


def test_flux_generated_kustomization_scans_like_kustomize_controller(tmp_path: Path) -> None:
    for rel in ["app.k8s.yaml", "notes.md", "config.json", "plain/secret.sops.yaml", "overlay/kustomization.yaml"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("")

    kust = flux_generated_kustomization(tmp_path)

    # A subdirectory with its own kustomization is one resource; any other YAML file,
    # nested or not, is its own resource; non-YAML files are not resources.
    assert set(kust.resources) == {Path("app.k8s.yaml"), Path("overlay"), Path("plain/secret.sops.yaml")}


if __name__ == "__main__":
    pytest_bazel.main()
