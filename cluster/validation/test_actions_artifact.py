"""Contracts for source-artifact migrations."""

import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.validation.tool_resolve import resolve_tool
from util.bazel.runfiles import get_required_path


def _assert_copy_source_in_checkout(path: str, source: dict, artifact_name: str) -> None:
    if directories := source["spec"].get("sparseCheckout"):
        assert any(PurePosixPath(path).is_relative_to(directory) for directory in directories), (
            f"{artifact_name}: copy source {path} is outside GitRepository "
            f"{source['metadata']['namespace']}/{source['metadata']['name']}'s sparse checkout"
        )


@pytest.mark.parametrize(
    ("directories", "path", "included"),
    [
        (None, "haku/runtime/managed_agent/self_hosted/deploy", True),
        ([], "haku/runtime/managed_agent/self_hosted/deploy", True),
        (["cluster/k8s/"], "cluster/k8s", True),
        (["cluster/k8s/"], "cluster/k8s/haku/console", True),
        (["cluster/k8s/"], "cluster/k8s-extra", False),
        (["cluster/k8s/"], "haku/runtime/managed_agent/self_hosted/deploy", False),
        (
            ["cluster/k8s/", "haku/runtime/managed_agent/self_hosted/deploy/"],
            "haku/runtime/managed_agent/self_hosted/deploy",
            True,
        ),
    ],
)
def test_copy_source_sparse_checkout(directories: list[str] | None, path: str, included: bool) -> None:
    source: dict = {"metadata": {"namespace": "ducktape-flux", "name": "ducktape"}, "spec": {}}
    if directories is not None:
        source["spec"]["sparseCheckout"] = directories
    if included:
        _assert_copy_source_in_checkout(path, source, "example-artifact")
    else:
        with pytest.raises(AssertionError, match=r"example-artifact: copy source .* is outside GitRepository"):
            _assert_copy_source_in_checkout(path, source, "example-artifact")


def test_artifact_generators_preserve_render_inputs(tmp_path: Path) -> None:
    """Every generated artifact has one declared consumer and preserves its Kustomize output."""
    root = get_required_path("_main/cluster/k8s/kustomization.yaml").parent.resolve()
    repository_root = root.parent.parent
    kustomize = resolve_tool("kustomize", "multitool/tools/kustomize/kustomize")
    git_sources = {
        (document["metadata"]["namespace"], document["metadata"]["name"]): document
        for path in root.rglob("*.yaml")
        # Authentik blueprints use custom YAML tags and are not Kubernetes resources.
        if "blueprints" not in path.parts
        for document in yaml.safe_load_all(path.read_text())
        if isinstance(document, dict) and document.get("kind") == "GitRepository"
    }

    generators = [
        document
        for document in yaml.safe_load_all((root / "artifact-generators/artifact-generators.k8s.yaml").read_text())
        if document and document.get("kind") == "ArtifactGenerator"
    ]
    artifact_names = [artifact["name"] for generator in generators for artifact in generator["spec"]["artifacts"]]
    assert len(artifact_names) == len(set(artifact_names))
    generated_artifacts = {
        artifact["name"]: (generator, artifact)
        for generator in generators
        for artifact in generator["spec"]["artifacts"]
    }

    # The conventional parked tree keeps consumer declarations without packaging them.
    consumers: dict[str, list[tuple[Path, dict]]] = {}
    flux_chart = root / "flux/kustomizations.k8s.yaml"
    for document in yaml.safe_load_all(flux_chart.read_text()):
        if not isinstance(document, dict) or document.get("kind") != "Kustomization":
            continue
        source = document["spec"].get("sourceRef", {})
        if source.get("kind") != "ExternalArtifact":
            continue
        relative = document["spec"]["path"].removeprefix("./")
        consumer_dir = (repository_root / relative).resolve()
        if Path(relative).parts[:3] == ("cluster", "k8s", "parked"):
            continue
        consumers.setdefault(source["name"], []).append((consumer_dir, document))
    assert set(consumers) == set(generated_artifacts)

    for artifact_name, (generator, artifact) in generated_artifacts.items():
        consumer_dir, consumer = one(consumers[artifact_name])
        relative = consumer["spec"]["path"].removeprefix("./")
        assert consumer_dir == (repository_root / relative).resolve()
        aliases = {source["alias"] for source in generator["spec"]["sources"]}
        assert consumer["spec"]["sourceRef"] == {
            "kind": "ExternalArtifact",
            "name": artifact_name,
            "namespace": generator["metadata"]["namespace"],
        }
        assert consumer["spec"]["path"] == f"./{relative}"
        assert "revision" not in artifact  # Content-derived, not the monorepo revision.
        alias = artifact["originRevision"].removeprefix("@")
        assert alias in aliases
        primary_operation = one(
            operation for operation in artifact["copy"] if operation["to"] == f"@artifact/{relative}/"
        )
        assert primary_operation == {"from": f"@{alias}/{relative}/**", "to": f"@artifact/{relative}/"}
        packaged_root = tmp_path / artifact_name
        packaged = packaged_root / relative
        for operation in artifact["copy"]:
            copy_alias, _, copy_path = operation["from"].removeprefix("@").partition("/")
            assert copy_alias in aliases, f"{artifact_name}: unknown copy source alias {copy_alias}"
            operation_source_relative = copy_path.removesuffix("/**")
            source_ref = one(source for source in generator["spec"]["sources"] if source["alias"] == copy_alias)
            assert source_ref["kind"] == "GitRepository"
            source_key = (source_ref.get("namespace", generator["metadata"]["namespace"]), source_ref["name"])
            assert source_key in git_sources, f"{artifact_name}: missing GitRepository {source_key}"
            # The local checkout is broader than the source-controller artifact. Reject unavailable
            # inputs before copying them, otherwise a successful local build hides a live failure.
            _assert_copy_source_in_checkout(operation_source_relative, git_sources[source_key], artifact_name)
            operation_source = repository_root / operation_source_relative
            operation_target = packaged_root / operation["to"].removeprefix("@artifact/")
            shutil.copytree(
                operation_source, operation_target, ignore=shutil.ignore_patterns(*operation.get("exclude", []))
            )
        assert not (packaged / "flux-kustomization.yaml").exists()
        original = subprocess.run([kustomize, "build", str(consumer_dir)], check=True, capture_output=True, text=True)
        rebuilt = subprocess.run([kustomize, "build", str(packaged)], check=True, capture_output=True, text=True)
        assert list(yaml.safe_load_all(rebuilt.stdout)) == list(yaml.safe_load_all(original.stdout))


if __name__ == "__main__":
    pytest_bazel.main()
