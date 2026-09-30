import functools
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.artifact_generators import artifact
from cluster.cdk8s.flux import (
    GeneratorOptions,
    Json6902Patch,
    PatchTarget,
    artifact_directory,
    flux_kustomization,
    kustomizations_chart,
)
from cluster.cdk8s.generation import config_map_chart, write_directory

_ARTIFACT = artifact("test-app", "test/app")


def _chart(name: str) -> Callable[[App], Chart]:
    return functools.partial(config_map_chart, chart_name=name, configmap_name=name, namespace="test-ns", data={})


def test_kustomization_lists_each_synthesized_file_then_the_siblings(tmp_path: Path) -> None:
    write_directory(tmp_path, _ARTIFACT, _chart("first"), _chart("second"), siblings=["test-secret.sops.yaml"])
    out_dir = tmp_path / artifact_directory(_ARTIFACT)
    written = sorted(path.name for path in out_dir.iterdir() if path.name != "kustomization.yaml")
    assert len(written) == 2
    # The sibling is hand-written, so absent here.
    assert yaml.safe_load((out_dir / "kustomization.yaml").read_text())["resources"] == [
        *written,
        "test-secret.sops.yaml",
    ]


def test_patch_charts_are_listed_as_patches_not_resources(tmp_path: Path) -> None:
    target = PatchTarget(kind="Deployment", name="test-app", namespace="test-ns")
    write_directory(
        tmp_path,
        _ARTIFACT,
        _chart("first"),
        remote_resources=["https://release.test/app.yaml"],
        patch_charts=[_chart("patch")],
        json6902_patches=[Json6902Patch(patch="[]", target=target)],
    )
    kustomization = yaml.safe_load((tmp_path / "test/app/kustomization.yaml").read_text())
    assert kustomization["resources"] == ["first.k8s.yaml", "https://release.test/app.yaml"]
    assert kustomization["patches"] == [
        {"path": "patch.k8s.yaml"},
        {"patch": "[]", "target": {"kind": "Deployment", "name": "test-app", "namespace": "test-ns"}},
    ]


@pytest.mark.parametrize(
    ("siblings", "decrypted"),
    [((), False), (("test-config.yaml",), False), (("test-config.yaml", "test-secret.sops.yaml"), True)],
)
def test_flux_decrypts_exactly_when_a_sibling_is_sops(tmp_path: Path, siblings: Sequence[str], decrypted: bool) -> None:
    chart = kustomizations_chart(Cdk8sTesting.app())
    flux_kustomization(chart, "test-app", write_directory(tmp_path, _ARTIFACT, _chart("first"), siblings=siblings))
    (rendered,) = Cdk8sTesting.synth(chart)
    assert ("decryption" in rendered["spec"]) is decrypted


def test_writer_refuses_an_artifact_with_shared_bases(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="also copies"):
        write_directory(tmp_path, artifact("test-app", "test/app", "test/base"), _chart("first"))


def test_writer_accepts_a_copied_component_the_directory_includes(tmp_path: Path) -> None:
    write_directory(tmp_path, artifact("test-app", "test/app", "test/pins"), _chart("first"), components=["../pins"])
    assert yaml.safe_load((tmp_path / "test/app/kustomization.yaml").read_text())["components"] == ["../pins"]


def test_directory_generator_options_are_written_under_kustomizes_key(tmp_path: Path) -> None:
    write_directory(
        tmp_path, _ARTIFACT, _chart("first"), generator_options=GeneratorOptions(disable_name_suffix_hash=True)
    )
    kustomization = yaml.safe_load((tmp_path / "test/app/kustomization.yaml").read_text())
    assert kustomization["generatorOptions"] == {"disableNameSuffixHash": True}


if __name__ == "__main__":
    pytest_bazel.main()
