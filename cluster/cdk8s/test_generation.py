import functools
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.artifact_generators import artifact
from cluster.cdk8s.flux import artifact_directory, flux_kustomization, kustomizations_chart
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
    with pytest.raises(ValueError, match="shared bases"):
        write_directory(tmp_path, artifact("test-app", "test/app", "test/base"), _chart("first"))


if __name__ == "__main__":
    pytest_bazel.main()
